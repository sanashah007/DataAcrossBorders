"""Reading contracts/ off disk into an enforceable registry.

Loading happens once at gateway startup, not as a build step, for three reasons:

* The digest is taken over the YAML bytes, so the YAML is the authority. A
  compiled artifact checked into the repo would be a second thing that can be
  wrong, and it would look authoritative while being stale.
* Validation needs to know which columns the federation can actually serve. A
  build step cannot know that; startup can.
* It is free — a handful of small documents, parsed once.

Nothing here fails the process. A contract that does not parse, or that names a
column that does not exist, is *rejected and skipped* with a loud message, and
the rest of the registry loads. A single bad contract must not take the gateway
down and with it every other contract's traffic.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from gateway.contracts.engine import CompiledContract, ContractError, compile_contract
from gateway.contracts.expr import PredicateError
from gateway.contracts.spec import ContractSpec

__all__ = ["ContractRegistry", "load_registry"]


@dataclass(frozen=True)
class Acceptance:
    """One hospital's agreement to serve data under one contract version."""

    node: str
    contract: str
    version: str
    digest: str
    accepted_on: Optional[str] = None
    accepted_by: Optional[str] = None

    @property
    def ref(self) -> str:
        return f"{self.contract}@{self.version}"


@dataclass
class ContractRegistry:
    """Every contract the gateway knows, with who accepted it and who may use it."""

    contracts: dict[str, CompiledContract] = field(default_factory=dict)
    grants: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Problems found at load time, surfaced on /api/contracts for debugging.
    problems: list[str] = field(default_factory=list)

    def get(self, ref: str) -> Optional[CompiledContract]:
        return self.contracts.get(ref)

    def granted_to(self, username: str) -> frozenset[str]:
        return self.grants.get(username, frozenset())

    def is_granted(self, username: str, ref: str) -> bool:
        return ref in self.granted_to(username)

    def refs(self) -> list[str]:
        return sorted(self.contracts)


def _digest(path: Path) -> str:
    """sha256 over the exact bytes on disk — whitespace and comments included.

    Anything less would let a contract be edited in ways a committee did not
    review while still matching what it signed.
    """
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _warn(problems: list[str], message: str) -> None:
    problems.append(message)
    print(f"CONTRACT: {message}", file=sys.stderr)


def _load_yaml(path: Path) -> Any:
    with path.open() as handle:
        return yaml.safe_load(handle)


def _load_acceptance(directory: Path, problems: list[str]) -> list[Acceptance]:
    """Read every hospital's acceptance list.

    A hospital with no file has accepted nothing, which is the safe reading of
    silence — it does not mean "accepts everything".
    """
    accepted: list[Acceptance] = []
    if not directory.is_dir():
        _warn(problems, f"no acceptance directory at {directory}; no node will serve data")
        return accepted

    for path in sorted(directory.glob("*.yaml")):
        try:
            document = _load_yaml(path) or {}
            node = str(document["node"]).upper()
            for entry in document.get("accepted") or []:
                accepted.append(
                    Acceptance(
                        node=node,
                        contract=str(entry["contract"]),
                        version=str(entry["version"]),
                        digest=str(entry["digest"]),
                        accepted_on=str(entry.get("accepted_on") or "") or None,
                        accepted_by=entry.get("accepted_by"),
                    )
                )
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            _warn(problems, f"{path.name} is malformed and was ignored: {exc}")
    return accepted


def _load_grants(path: Path, problems: list[str]) -> dict[str, frozenset[str]]:
    if not path.is_file():
        _warn(problems, f"no grants file at {path}; no user may invoke any contract")
        return {}
    try:
        document = _load_yaml(path) or {}
        return {
            str(user): frozenset(str(ref) for ref in refs or [])
            for user, refs in (document.get("grants") or {}).items()
        }
    except (AttributeError, TypeError, yaml.YAMLError) as exc:
        _warn(problems, f"{path.name} is malformed and was ignored: {exc}")
        return {}


def load_registry(
    directory: Path,
    *,
    available_columns: Optional[frozenset[str]] = None,
) -> ContractRegistry:
    """Load, validate, and compile every contract under `directory`."""
    problems: list[str] = []
    registry = ContractRegistry(problems=problems)

    if not directory.is_dir():
        _warn(problems, f"no contracts directory at {directory}; all queries will 404")
        return registry

    acceptances = _load_acceptance(directory / "acceptance", problems)
    registry.grants = _load_grants(directory / "grants.yaml", problems)

    by_ref: dict[str, list[Acceptance]] = {}
    for acceptance in acceptances:
        by_ref.setdefault(acceptance.ref, []).append(acceptance)

    for path in sorted(directory.glob("*.yaml")):
        if path.name == "grants.yaml":
            continue
        try:
            spec = ContractSpec.model_validate(_load_yaml(path))
        except (ValidationError, yaml.YAMLError) as exc:
            _warn(problems, f"{path.name} is not a valid contract and was skipped: {exc}")
            continue

        if spec.contract != path.stem:
            _warn(
                problems,
                f"{path.name} declares contract id '{spec.contract}'; the filename must "
                f"match so a digest can be traced back to a document. Skipped.",
            )
            continue

        digest = _digest(path)
        try:
            compiled = compile_contract(spec, digest, available_columns=available_columns)
        except (ContractError, PredicateError) as exc:
            _warn(problems, f"{path.name} was rejected: {exc}")
            continue

        accepted_nodes = _resolve_acceptance(spec.ref, digest, by_ref, problems)
        compiled = _with_nodes(compiled, accepted_nodes)

        if compiled.expired:
            _warn(problems, f"contract '{spec.ref}' expired on {spec.expires}; it will 403")
        if compiled.unavailable:
            _warn(
                problems,
                f"contract '{spec.ref}' names column(s) no node serves yet: "
                f"{', '.join(sorted(compiled.unavailable))}. They will simply not appear.",
            )
        if not accepted_nodes:
            _warn(problems, f"contract '{spec.ref}' has been accepted by no hospital")

        registry.contracts[spec.ref] = compiled

    _warn_unknown_grants(registry, problems)
    return registry


def _resolve_acceptance(
    ref: str,
    digest: str,
    by_ref: dict[str, list[Acceptance]],
    problems: list[str],
) -> frozenset[str]:
    """Keep only acceptances whose pinned digest still matches the file.

    A mismatch means the contract was edited after the hospital agreed to it, so
    what they agreed to no longer exists. Treating that as "still accepted" would
    let anyone widen a contract by editing a file.
    """
    nodes: set[str] = set()
    for acceptance in by_ref.get(ref, []):
        if acceptance.digest != digest:
            _warn(
                problems,
                f"{acceptance.node} accepted '{ref}' at digest {acceptance.digest[:19]}… "
                f"but the file now hashes to {digest[:19]}…. The document changed after "
                f"it was agreed to, so {acceptance.node} is treated as not having "
                f"accepted it. Bump the version and re-approve.",
            )
            continue
        nodes.add(acceptance.node)
    return frozenset(nodes)


def _with_nodes(compiled: CompiledContract, nodes: frozenset[str]) -> CompiledContract:
    """CompiledContract is frozen, so acceptance is attached by rebuilding it."""
    return CompiledContract(
        spec=compiled.spec,
        digest=compiled.digest,
        released=compiled.released,
        denied=compiled.denied,
        row_scope=compiled.row_scope,
        accepted_nodes=nodes,
        unavailable=compiled.unavailable,
    )


def _warn_unknown_grants(registry: ContractRegistry, problems: list[str]) -> None:
    """A grant naming a contract that does not exist is almost always a typo."""
    for username, refs in registry.grants.items():
        for ref in sorted(refs):
            if ref not in registry.contracts:
                _warn(
                    problems,
                    f"grants.yaml gives '{username}' contract '{ref}', which does not "
                    f"exist or failed to load",
                )
