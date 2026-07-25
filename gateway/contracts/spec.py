"""The shape of a contract document, and the column registry it is checked against.

This module is the machine-readable half of `contracts/README.md`. If the two
ever disagree, the README is what a hospital committee actually agreed to, so
fix this file.

Two things live here:

* `ContractSpec` / `Rule` — Pydantic models mirroring the YAML, with validation
  strict enough that a malformed contract fails at startup rather than at query
  time. A contract that cannot be understood is never a contract that gets used.
* The **column registry** — the universe of column names a contract may name,
  and which of them are derived from others.

The derivation registry exists to answer one question: does releasing column X
also release column Y that the contract denied? See `Derivation.reversible`.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models import LabeledStudyRecord, StudyRecord

__all__ = [
    "ALL_COLUMNS",
    "DERIVED",
    "GATEWAY_COLUMNS",
    "LABELED_COLUMNS",
    "NODE_COLUMNS",
    "ContractSpec",
    "Derivation",
    "Rule",
]

# Columns the hospital nodes actually serve today.
NODE_COLUMNS: frozenset[str] = frozenset(StudyRecord.model_fields)

# Columns the gateway attaches on the way through — see gateway/schemas.py.
GATEWAY_COLUMNS: frozenset[str] = frozenset({"SourceNode", "FederatedID"})

# Columns the labeling pipeline (scripts/label_studies.py) derives from the
# free-text Diagnosis. Contracts may name these before the pipeline has run; the
# loader warns and carries on rather than refusing to start.
LABELED_COLUMNS: frozenset[str] = frozenset(
    set(LabeledStudyRecord.model_fields) - set(StudyRecord.model_fields)
)

ALL_COLUMNS: frozenset[str] = NODE_COLUMNS | GATEWAY_COLUMNS | LABELED_COLUMNS


class Derivation(BaseModel):
    """Records that one column is computed from others.

    `reversible` is the whole point. A reversible derivation contains its sources
    verbatim, so releasing it releases them too — `FederatedID` is "BCH:BR-7214",
    which is `StudyID` with the node glued on. A contract that allows a reversible
    column while denying any of its sources is rejected at load time.

    A non-reversible ("declassifying") derivation is lossy by construction.
    `GenericCategory` reduces a multi-paragraph report to one of nine values, and
    releasing it *instead of* `Diagnosis` is exactly what the labeling pipeline is
    for. Those are allowed to outlive a denial of their source.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[str, ...]
    reversible: bool
    note: str = ""


DERIVED: dict[str, Derivation] = {
    "FederatedID": Derivation(
        sources=("SourceNode", "StudyID"),
        reversible=True,
        note="literally SourceNode + ':' + StudyID",
    ),
    "GenericCategory": Derivation(
        sources=("Diagnosis",),
        reversible=False,
        note="one of nine fixed categories; retains no free text",
    ),
    "FindingTags": Derivation(
        sources=("Diagnosis",),
        reversible=False,
        note="structured, negation-aware tags; retains no free text",
    ),
}

_CONTRACT_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _as_list(value: Union[str, list[str], None]) -> list[str]:
    """`allow: Diagnosis` and `allow: [Diagnosis]` mean the same thing."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class Rule(BaseModel):
    """One `allow:` or `deny:` clause.

    Exactly one of the two is set. `when:` narrows an `allow` to the rows where a
    predicate holds; it is meaningless on a `deny`, because a column denied on
    only some rows is a column that leaks which rows those are.
    """

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    when: Optional[str] = None
    reason: Optional[str] = None
    # Reserved for the transform work (bin / truncate / redact). Accepted by the
    # parser so the field name is stable, rejected below so a contract author is
    # never quietly told "yes" to a restriction the engine does not apply.
    transform: Optional[dict] = None

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _coerce_to_list(cls, value):
        return _as_list(value)

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if bool(self.allow) == bool(self.deny):
            raise ValueError("a rule must set exactly one of `allow:` or `deny:`")
        if self.when and self.deny:
            raise ValueError(
                "`when:` cannot be used with `deny:` — a column denied on only some "
                "rows reveals which rows those are. Use `row_scope:` to hide rows."
            )
        if self.transform is not None:
            raise ValueError(
                "`transform:` is not implemented yet. Remove it, or deny the column: "
                "a contract must not appear to restrict something the engine ignores."
            )
        for column in [*self.allow, *self.deny]:
            if column not in ALL_COLUMNS:
                raise ValueError(
                    f"unknown column {column!r}. Known columns: "
                    f"{', '.join(sorted(ALL_COLUMNS))}"
                )
        return self

    @property
    def columns(self) -> list[str]:
        return self.allow or self.deny


class ContractSpec(BaseModel):
    """A whole contract document, as parsed from YAML.

    Validation here is structural only — it does not know which columns a node
    actually serves, nor whether any hospital accepted this. Those checks belong
    to the loader, which has the runtime context.
    """

    model_config = ConfigDict(extra="forbid")

    contract: str
    version: str
    title: str
    purpose: str
    expires: Optional[date] = None
    row_scope: Optional[str] = None
    rules: list[Rule] = Field(min_length=1)

    @field_validator("contract")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _CONTRACT_ID.match(value):
            raise ValueError(
                f"contract id {value!r} must be lowercase kebab-case, e.g. "
                f"'fetal-cardiac-outcomes'"
            )
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not _SEMVER.match(value):
            raise ValueError(f"version {value!r} must be semver, e.g. '1.2.0'")
        return value

    @property
    def ref(self) -> str:
        """The `id@version` string a requester names in `?contract=`."""
        return f"{self.contract}@{self.version}"
