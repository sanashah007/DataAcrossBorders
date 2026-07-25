"""The predicate language used by contract `when:` and `row_scope:` clauses.

Contract documents are written by governance committees, not developers, and are
loaded from disk at startup. That makes them a code-execution surface, so this
module deliberately does not use `eval()`, `exec()`, or `compile()` at any point.

A predicate is parsed with `ast.parse(mode="eval")`, checked against a whitelist
of syntax node types, and then walked by a hand-written evaluator that knows how
to do exactly six things: read a column, read a literal, compare, combine with
and/or/not, build a list, and nothing else. A predicate that parses but contains
a function call, an attribute access, arithmetic, an f-string, or a comprehension
is rejected at load time with the offending construct named.

The grammar, in full:

    <column>                      PatientSex, BodyPartExamined, ...
    "literal" / 123 / true        constants
    == != < <= > >=               comparisons, chainable (a < b < c)
    in / not in                   membership against a list literal
    and / or / not                boolean combination
    ( ... )                       grouping

Missing columns evaluate conservatively. If a predicate references a column the
record does not carry — `GenericCategory` before the labeling pipeline has run —
every comparison involving it is False, so a `when:` clause withholds the column
rather than releasing it by accident.
"""

from __future__ import annotations

import ast
from typing import Any, Callable, Mapping

__all__ = ["Predicate", "PredicateError", "compile_predicate"]


class PredicateError(ValueError):
    """A predicate could not be parsed, or used a construct outside the grammar."""


class _Missing:
    """Sentinel for a column the record does not carry.

    Distinct from None, which is a legitimate (if currently unused) column value.
    Any comparison involving this returns False — see `_compare`.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING = _Missing()

# Node types the grammar permits. Anything else is rejected by name, so a
# contract author gets "Call is not allowed" rather than a stack trace.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
)

# Literals Python parses as names rather than constants in older grammars, and
# the lowercase spellings a YAML author is likely to reach for.
_NAME_CONSTANTS = {"true": True, "false": False, "null": None, "none": None}


class Predicate:
    """A compiled, callable contract predicate.

    Call it with a record mapping to get a bool. `columns` lists every column the
    predicate reads, which the loader uses to reject references to columns that
    do not exist in the schema.
    """

    __slots__ = ("source", "columns", "_tree")

    def __init__(self, source: str, columns: frozenset[str], tree: ast.expr):
        self.source = source
        self.columns = columns
        self._tree = tree

    def __call__(self, row: Mapping[str, Any]) -> bool:
        return bool(_eval(self._tree, row))

    def __repr__(self) -> str:
        return f"Predicate({self.source!r})"


def compile_predicate(source: str) -> Predicate:
    """Parse and validate `source`, returning a callable `Predicate`.

    Raises `PredicateError` with a human-readable message on anything the
    grammar does not permit. Never executes the expression.
    """
    if not isinstance(source, str) or not source.strip():
        raise PredicateError("predicate is empty")

    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise PredicateError(f"{source!r} is not a valid expression: {exc.msg}") from exc

    columns: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise PredicateError(
                f"{type(node).__name__} is not allowed in a contract predicate "
                f"(in {source!r}). The grammar permits column names, literals, "
                f"comparisons, in/not in, and and/or/not."
            )
        if isinstance(node, ast.Name) and node.id.lower() not in _NAME_CONSTANTS:
            columns.add(node.id)

    return Predicate(source, frozenset(columns), tree.body)


def _eval(node: ast.expr, row: Mapping[str, Any]) -> Any:
    """Walk a whitelisted tree. Every branch here is a grammar production."""
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id.lower() in _NAME_CONSTANTS:
            return _NAME_CONSTANTS[node.id.lower()]
        return row.get(node.id, MISSING)

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(element, row) for element in node.elts]

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, row)

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(value, row) for value in node.values)
        return any(_eval(value, row) for value in node.values)

    if isinstance(node, ast.Compare):
        # Chained comparisons (a < b < c) share operands pairwise, exactly as
        # Python does, and short-circuit on the first False.
        left = _eval(node.left, row)
        for op, comparator_node in zip(node.ops, node.comparators):
            right = _eval(comparator_node, row)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    # Unreachable: compile_predicate whitelisted every node before we got here.
    raise PredicateError(f"cannot evaluate {type(node).__name__}")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    """Apply one comparison, treating a missing column as unsatisfiable.

    A record that lacks the column cannot be said to match *or* not match, and
    guessing in the permissive direction would release data. So every comparison
    touching MISSING is False, including `!=`, which would otherwise be True and
    would quietly release a column for every record lacking it.
    """
    if left is MISSING or right is MISSING:
        return False

    if isinstance(op, ast.In):
        return _contains(left, right)
    if isinstance(op, ast.NotIn):
        return not _contains(left, right)

    try:
        if isinstance(op, ast.Eq):
            return bool(left == right)
        if isinstance(op, ast.NotEq):
            return bool(left != right)
        if isinstance(op, ast.Lt):
            return bool(left < right)
        if isinstance(op, ast.LtE):
            return bool(left <= right)
        if isinstance(op, ast.Gt):
            return bool(left > right)
        if isinstance(op, ast.GtE):
            return bool(left >= right)
    except TypeError:
        # Comparing a string column against a number, say. Unsatisfiable rather
        # than a 500 at query time.
        return False

    raise PredicateError(f"unsupported comparison {type(op).__name__}")


def _contains(needle: Any, haystack: Any) -> bool:
    if not isinstance(haystack, (list, tuple, set, frozenset, str)):
        return False
    try:
        return needle in haystack
    except TypeError:
        return False
