"""Safe rule-expression compiler (no ``eval``/``exec``).

A rule's ``when`` clause (e.g. ``is_new_payee and amount_ratio >= 5``) is
parsed with :mod:`ast`, checked against a strict node whitelist and compiled
into a tree of Python closures. Only feature names, numeric/string/bool
literals, arithmetic, comparisons, boolean logic, literal tuples/lists (for
``in``) and a few whitelisted functions are allowed — no attribute access, no
subscripts, no comprehensions, no lambdas, no arbitrary calls. Unknown names
are rejected at compile time, so a typo in a rule fails on load, not in prod.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

Env = Mapping[str, Any]
Node = Callable[[Env], Any]

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: lambda a, b: a / b if b else 0.0,  # division by zero -> 0, never raise
    ast.FloorDiv: lambda a, b: a // b if b else 0.0,
    ast.Mod: lambda a, b: a % b if b else 0.0,
}
_CMPOPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "log1p": math.log1p,
    "clamp": lambda x, lo, hi: max(lo, min(hi, x)),
}
_MAX_LENGTH = 500
_MAX_NODES = 200


class ExpressionError(ValueError):
    """Invalid or disallowed rule expression."""


@dataclass(frozen=True)
class CompiledExpression:
    source: str
    names: frozenset[str]
    _fn: Node

    def __call__(self, env: Env) -> Any:
        return self._fn(env)

    def evaluate_bool(self, env: Env) -> bool:
        try:
            return bool(self._fn(env))
        except (TypeError, ValueError, ArithmeticError):
            return False


def compile_expression(source: str, allowed_names: Iterable[str]) -> CompiledExpression:
    """Parse, validate and compile ``source``; raise :class:`ExpressionError`."""
    if not source or not source.strip():
        raise ExpressionError("boş ifade")
    if len(source) > _MAX_LENGTH:
        raise ExpressionError(f"ifade çok uzun (> {_MAX_LENGTH} karakter)")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"sözdizimi hatası: {exc.msg}") from None
    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ExpressionError("ifade çok karmaşık")
    allowed = frozenset(allowed_names)
    names: set[str] = set()
    fn = _compile(tree.body, allowed, names)
    return CompiledExpression(source.strip(), frozenset(names), fn)


def _compile(node: ast.AST, allowed: frozenset[str], names: set[str]) -> Node:
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, int | float | str | bool | type(None)):
            raise ExpressionError(f"izin verilmeyen sabit: {node.value!r}")
        value = node.value
        return lambda env: value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):  # pragma: no cover - py<3.8 style
            raise ExpressionError("sabitleri küçük harf kullanmayın")
        if node.id not in allowed:
            raise ExpressionError(f"bilinmeyen alan: '{node.id}'")
        names.add(node.id)
        key = node.id
        return lambda env: env.get(key, 0.0)
    if isinstance(node, ast.BoolOp):
        parts = [_compile(v, allowed, names) for v in node.values]
        if isinstance(node.op, ast.And):
            return lambda env: all(p(env) for p in parts)
        return lambda env: any(p(env) for p in parts)
    if isinstance(node, ast.UnaryOp):
        operand = _compile(node.operand, allowed, names)
        if isinstance(node.op, ast.Not):
            return lambda env: not operand(env)
        if isinstance(node.op, ast.USub):
            return lambda env: -operand(env)
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ExpressionError("izin verilmeyen tekli operatör")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"izin verilmeyen operatör: {type(node.op).__name__}")
        left, right = _compile(node.left, allowed, names), _compile(node.right, allowed, names)
        return lambda env: op(left(env), right(env))
    if isinstance(node, ast.Compare):
        first = _compile(node.left, allowed, names)
        ops = []
        for cmp, comparator in zip(node.ops, node.comparators, strict=True):
            fn = _CMPOPS.get(type(cmp))
            if fn is None:
                raise ExpressionError(f"izin verilmeyen karşılaştırma: {type(cmp).__name__}")
            ops.append((fn, _compile(comparator, allowed, names)))

        def compare(env: Env) -> bool:
            left_value = first(env)
            for fn, right in ops:
                right_value = right(env)
                if not fn(left_value, right_value):
                    return False
                left_value = right_value
            return True

        return compare
    if isinstance(node, ast.IfExp):
        test = _compile(node.test, allowed, names)
        body, orelse = _compile(node.body, allowed, names), _compile(node.orelse, allowed, names)
        return lambda env: body(env) if test(env) else orelse(env)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        items = [_compile(e, allowed, names) for e in node.elts]
        return lambda env: tuple(i(env) for i in items)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise ExpressionError("yalnızca min/max/abs/log1p/clamp çağrılabilir")
        if node.keywords:
            raise ExpressionError("anahtar kelimeli argüman desteklenmez")
        func = FUNCTIONS[node.func.id]
        args = [_compile(a, allowed, names) for a in node.args]
        return lambda env: func(*(a(env) for a in args))
    raise ExpressionError(f"izin verilmeyen yapı: {type(node).__name__}")
