"""Resolving a contract document into something that can decide about records.

`ContractSpec` describes what a contract *says*. `CompiledContract` is what it
*does*: which columns come out, which rows exist, and which queries are legal to
ask under it.

Three ideas are worth knowing before reading the code.

**Default-deny, deny-overrides.** A column named by no rule is not released. A
column named by both `allow` and `deny` is not released, in either order. So
appending a rule can never widen a contract, which is the property that makes
these documents safe to review by eye.

**Unconditional vs conditional release.** `allow: X` releases a column on every
row; `allow: X when: <predicate>` releases it only on matching rows. Only
unconditionally released columns may be filtered or sorted on — see
`validate_query` for why.

**Projection happens last.** Row predicates, filters, and statistics all need the
underlying values, so records stay whole until the final step of a request.
Nothing between `scope_rows` and `project` may be returned to a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Optional

from gateway.contracts.expr import Predicate, compile_predicate
from gateway.contracts.spec import ALL_COLUMNS, DERIVED, ContractSpec

__all__ = ["ContractError", "CompiledContract", "compile_contract"]


class ContractError(ValueError):
    """A contract is structurally valid YAML but does not describe a usable policy."""


# Which record column each search parameter reads. `validate_query` uses this to
# refuse a query that would filter on a column the contract does not release —
# the redaction-bypass oracle described in gateway/README.md.
QUERY_COLUMNS: dict[str, str] = {
    "body_part": "BodyPartExamined",
    "modality": "Modality",
    "sex": "PatientSex",
    "min_age": "PatientAge",
    "max_age": "PatientAge",
    "study_date_from": "StudyDate",
    "study_date_to": "StudyDate",
}

# The columns free-text `q` searches, in gateway/filters.py `_matches_text`.
TEXT_SEARCH_COLUMNS: tuple[str, ...] = (
    "Diagnosis",
    "PatientName",
    "StudyID",
    "PatientID",
)

# Preference order when a contract does not release the default sort column.
_SORT_FALLBACKS: tuple[str, ...] = ("StudyDate", "FederatedID", "StudyID", "PatientAge")


class QueryNotPermitted(ValueError):
    """A query asked to filter or sort on a column the contract does not release.

    Carries the column and the reason so the route can turn it into a 400 that
    tells the caller which part of their query was the problem.
    """

    def __init__(self, parameter: str, column: str, detail: str):
        self.parameter = parameter
        self.column = column
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CompiledContract:
    """A contract resolved against the schema and ready to enforce."""

    spec: ContractSpec
    digest: str
    #: Column -> predicate gating its release, or None when released outright.
    released: Mapping[str, Optional[Predicate]]
    #: Column -> the `reason:` of the rule that denied it, for explaining refusals.
    denied: Mapping[str, str]
    row_scope: Optional[Predicate] = None
    #: Nodes whose acceptance list includes this contract at this digest.
    accepted_nodes: frozenset[str] = frozenset()
    #: Columns named by the contract that no node currently serves.
    unavailable: frozenset[str] = frozenset()

    # ---- identity -------------------------------------------------------

    @property
    def ref(self) -> str:
        return self.spec.ref

    @property
    def expired(self) -> bool:
        return self.spec.expires is not None and self.spec.expires < date.today()

    # ---- column sets ----------------------------------------------------

    @property
    def unconditional_columns(self) -> frozenset[str]:
        """Released on every row — the only columns a query may filter or sort on."""
        return frozenset(c for c, pred in self.released.items() if pred is None)

    @property
    def conditional_columns(self) -> frozenset[str]:
        return frozenset(c for c, pred in self.released.items() if pred is not None)

    @property
    def max_columns(self) -> frozenset[str]:
        """Everything that could ever appear, i.e. the contract's widest output."""
        return frozenset(self.released)

    def releases(self, column: str) -> bool:
        """True if `column` appears on every released row.

        Statistics use this: an aggregate over a column that is only
        conditionally released would silently cover a subset of rows.
        """
        return self.released.get(column, "absent") is None

    # ---- enforcement ----------------------------------------------------

    def scope_rows(self, records: Iterable[Any]) -> list[Any]:
        """Drop rows outside `row_scope`.

        Applied before filtering, so `total` counts only rows the contract can
        see and cannot be used to infer the size of the data behind it.
        """
        if self.row_scope is None:
            return list(records)
        return [r for r in records if self.row_scope(_as_mapping(r))]

    def project(self, records: Iterable[Any]) -> list[dict[str, Any]]:
        """Reduce whole records to just the columns this contract releases.

        The final step of a request. Conditional columns are evaluated per row,
        so two rows in one response may legitimately carry different keys.
        """
        projected: list[dict[str, Any]] = []
        for record in records:
            row = _as_mapping(record)
            out: dict[str, Any] = {}
            for column, predicate in self.released.items():
                if column not in row:
                    continue  # labeled column the pipeline has not produced yet
                if predicate is None or predicate(row):
                    out[column] = row[column]
            projected.append(out)
        return projected

    def validate_query(
        self,
        *,
        q: Optional[str] = None,
        sort_by: Optional[str] = None,
        **params: Any,
    ) -> None:
        """Raise `QueryNotPermitted` if the query reads a column we do not release.

        Without this, a contract that hides `PatientName` still lets a caller
        binary-search for one via `?q=`, and the response's absence of the column
        is no protection at all.

        Conditionally released columns are refused outright rather than allowed
        for the rows where they apply: a whole-query predicate over a column that
        exists on only some rows discloses exactly which rows those are.
        """
        for parameter, value in params.items():
            if value is None or parameter not in QUERY_COLUMNS:
                continue
            column = QUERY_COLUMNS[parameter]
            self._require_unconditional(parameter, column)

        if q is not None:
            searchable = [c for c in TEXT_SEARCH_COLUMNS if c in self.unconditional_columns]
            if not searchable:
                raise QueryNotPermitted(
                    "q",
                    ", ".join(TEXT_SEARCH_COLUMNS),
                    f"Contract '{self.ref}' releases none of the free-text searchable "
                    f"columns ({', '.join(TEXT_SEARCH_COLUMNS)}), so `q` would search "
                    f"data it does not permit you to read.",
                )

        if sort_by is not None:
            self._require_unconditional("sort_by", sort_by)

    def searchable_columns(self) -> tuple[str, ...]:
        """The subset of the free-text columns `q` may actually search."""
        return tuple(c for c in TEXT_SEARCH_COLUMNS if c in self.unconditional_columns)

    def default_sort(self) -> Optional[str]:
        """A sort column this contract permits, or None to leave order untouched.

        The route's default sort must not 400 a caller who never asked to sort,
        so an unreleased default falls back rather than failing.
        """
        for candidate in _SORT_FALLBACKS:
            if candidate in self.unconditional_columns:
                return candidate
        return None

    def _require_unconditional(self, parameter: str, column: str) -> None:
        if column in self.unconditional_columns:
            return
        if column in self.conditional_columns:
            raise QueryNotPermitted(
                parameter,
                column,
                f"Contract '{self.ref}' releases '{column}' only on rows matching "
                f"`{self.released[column].source}`, so it cannot be used in "
                f"`{parameter}` — filtering on it would reveal which rows those are.",
            )
        reason = self.denied.get(column)
        raise QueryNotPermitted(
            parameter,
            column,
            f"Contract '{self.ref}' does not release '{column}', so `{parameter}` "
            f"cannot read it." + (f" Reason given: {reason}" if reason else ""),
        )


def _as_mapping(record: Any) -> Mapping[str, Any]:
    """Records are Pydantic models in the pipeline and dicts in tests."""
    if isinstance(record, Mapping):
        return record
    return record.model_dump()


def compile_contract(
    spec: ContractSpec,
    digest: str,
    *,
    available_columns: Optional[frozenset[str]] = None,
) -> CompiledContract:
    """Resolve a parsed contract into an enforceable one.

    `available_columns` is what the federation can actually serve right now. A
    contract naming a column outside it (typically `GenericCategory` before the
    labeling pipeline has run) is still usable — the column is recorded in
    `unavailable` and simply never appears.
    """
    available = ALL_COLUMNS if available_columns is None else available_columns

    denied: dict[str, str] = {}
    for rule in spec.rules:
        if rule.deny:
            for column in rule.deny:
                denied[column] = rule.reason or ""

    released: dict[str, Optional[Predicate]] = {}
    for rule in spec.rules:
        if not rule.allow:
            continue
        predicate = compile_predicate(rule.when) if rule.when else None
        if predicate is not None:
            _check_predicate_columns(predicate, spec)
        for column in rule.allow:
            if column in denied:
                continue  # deny overrides, wherever it appears
            # The most permissive allow wins: an unconditional grant elsewhere in
            # the document is not narrowed by a conditional one.
            if column in released and released[column] is None:
                continue
            released[column] = predicate

    row_scope = None
    if spec.row_scope:
        row_scope = compile_predicate(spec.row_scope)
        _check_predicate_columns(row_scope, spec)

    _check_reversible_derivations(released, denied, spec)

    return CompiledContract(
        spec=spec,
        digest=digest,
        released=released,
        denied=denied,
        row_scope=row_scope,
        unavailable=frozenset(c for c in released if c not in available),
    )


def _check_predicate_columns(predicate: Predicate, spec: ContractSpec) -> None:
    unknown = predicate.columns - ALL_COLUMNS
    if unknown:
        raise ContractError(
            f"contract '{spec.ref}' predicate `{predicate.source}` references unknown "
            f"column(s): {', '.join(sorted(unknown))}"
        )


def _check_reversible_derivations(
    released: Mapping[str, Optional[Predicate]],
    denied: Mapping[str, str],
    spec: ContractSpec,
) -> None:
    """Refuse a contract that hands over a denied column under another name.

    `FederatedID` is `SourceNode` + ':' + `StudyID`. Releasing it while denying
    `StudyID` is not a subtle privacy question, it is the denied value in the
    response. Declassifying derivations like `GenericCategory` are lossy and are
    deliberately exempt — releasing one in place of its source is the point.
    """
    for column in released:
        derivation = DERIVED.get(column)
        if derivation is None or not derivation.reversible:
            continue
        leaked = [s for s in derivation.sources if s in denied or s not in released]
        if leaked:
            raise ContractError(
                f"contract '{spec.ref}' releases '{column}', which contains "
                f"{', '.join(leaked)} verbatim, but does not release "
                f"{'them' if len(leaked) > 1 else 'it'}. Either release "
                f"{', '.join(leaked)} as well, or deny '{column}'."
            )
