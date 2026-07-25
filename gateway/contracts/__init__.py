"""Data contract loading and enforcement.

A contract declares which columns of the study schema may leave a hospital and
under what row conditions; hospitals independently record which contracts they
accepted. Every read request names one contract, and the gateway releases only
what it permits, from only the hospitals that accepted it.

The documents themselves live in `contracts/` at the repo root and are described
in `contracts/README.md`. This package turns them into decisions:

    spec.py     what a contract document may say, and the column registry
    expr.py     the `when:` / `row_scope:` predicate language (no eval)
    engine.py   resolving a document into released columns, rows, legal queries
    loader.py   reading contracts/ at startup, digests, acceptance, grants
"""

from gateway.contracts.engine import (
    CompiledContract,
    ContractError,
    QueryNotPermitted,
    compile_contract,
)
from gateway.contracts.expr import Predicate, PredicateError, compile_predicate
from gateway.contracts.loader import ContractRegistry, load_registry
from gateway.contracts.spec import ALL_COLUMNS, DERIVED, ContractSpec, Rule

__all__ = [
    "ALL_COLUMNS",
    "DERIVED",
    "CompiledContract",
    "ContractError",
    "ContractRegistry",
    "ContractSpec",
    "Predicate",
    "PredicateError",
    "QueryNotPermitted",
    "Rule",
    "compile_contract",
    "compile_predicate",
    "load_registry",
]
