"""Bounded AccessPlan operations and scalar contracts."""

from __future__ import annotations

import math
from typing import Any

from reframework_mcp.models import AccessNode, AccessOperation

_O = AccessOperation
LIVE_OPERATIONS = frozenset(
    {
        _O.RESOLVE_ROOT,
        _O.RESOLVE_TYPE,
        _O.READ_FIELD,
        _O.READ_PROPERTY,
        _O.CALL_METHOD,
        _O.CALL_STATIC,
        _O.BIND_CONSTANT,
        _O.CAST,
        _O.INDEX,
        _O.ITERATE,
        _O.FILTER,
        _O.ASSERT_NON_NULL,
        _O.ASSERT_TYPE,
        _O.ASSERT_RANGE,
        _O.EMIT,
    }
)
DRAFT_OPERATIONS = LIVE_OPERATIONS | {_O.CONSTRUCT_ARRAY, _O.CONSTRUCT_LIST, _O.CONSTRUCT_DICTIONARY}
INTEGER_BITS = {
    "System.SByte": (8, True),
    "System.Byte": (8, False),
    "System.Int16": (16, True),
    "System.UInt16": (16, False),
    "System.Int32": (32, True),
    "System.UInt32": (32, False),
    "System.Int64": (64, True),
    "System.UInt64": (64, False),
    "System.Char": (16, False),
}


def finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def scalar_error(value: Any, type_name: str | None, *, nullable: bool = True) -> str | None:
    if value is None:
        return None if nullable else "A non-null value is required"
    if type_name in INTEGER_BITS:
        bits, signed = INTEGER_BITS[type_name]
        lower, upper = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        if type(value) is not int or not lower <= value <= upper:
            return f"Expected {type_name} in [{lower}, {upper}]"
    elif type_name in {"System.Single", "System.Double"}:
        if not finite_number(value):
            return f"Expected a finite {type_name}"
        if type_name == "System.Single" and abs(value) > 3.4028234663852886e38:
            return "Value is outside the System.Single range"
    elif type_name == "System.Boolean" and type(value) is not bool:
        return "Expected a Boolean"
    elif type_name == "System.String" and not isinstance(value, str):
        return "Expected a String"
    return None


def node_error(node: AccessNode) -> str | None:
    options = node.options
    if (
        options.get("placeholder")
        or options.get("requires_user_value")
        or options.get("requires_user_population")
    ):
        return "Bind this node before validation or code generation"
    if node.operation not in DRAFT_OPERATIONS:
        return f"Unsupported operation: {node.operation.value}"
    if (
        node.operation
        in {
            _O.READ_FIELD,
            _O.READ_PROPERTY,
            _O.CALL_METHOD,
            _O.CAST,
            _O.INDEX,
            _O.ITERATE,
            _O.FILTER,
            _O.ASSERT_NON_NULL,
            _O.ASSERT_TYPE,
            _O.ASSERT_RANGE,
            _O.EMIT,
        }
        and len(node.inputs) != 1
    ):
        return "Operation requires exactly one input"
    if node.operation in {_O.CAST, _O.ASSERT_TYPE} and not (node.output_type or options.get("expected_type")):
        return "An expected type is required"
    if node.operation == _O.BIND_CONSTANT:
        return scalar_error(node.value, node.output_type, nullable=node.nullable)
    if node.operation == _O.ASSERT_RANGE:
        bounds = [options[key] for key in ("min", "max") if key in options]
        if not bounds or any(not finite_number(v) for v in bounds):
            return "assert_range needs finite min and/or max bounds"
        if "min" in options and "max" in options and options["min"] > options["max"]:
            return "min must not exceed max"
    if node.operation in {_O.ITERATE, _O.FILTER}:
        for key, default, maximum in (("offset", 0, 1_000_000), ("limit", 100, 1000)):
            value = options.get(key, default)
            if type(value) is not int or not (0 if key == "offset" else 1) <= value <= maximum:
                return f"{key} is outside the supported bounds"
    if node.operation == _O.INDEX and (type(options.get("index")) is not int or options["index"] < 0):
        return "index requires a non-negative integer index"
    if node.operation == _O.FILTER:
        if (
            options.get("operator", "eq") not in {"eq", "ne", "lt", "le", "gt", "ge"}
            or "value" not in options
        ):
            return "filter requires a comparison operator and value"
        if "field" in options and not isinstance(options["field"], str):
            return "filter field must be a member name"
    return None


def operation_support() -> dict[str, dict[str, bool]]:
    return {
        op.value: {
            "symbolic": op in DRAFT_OPERATIONS,
            "live": op in LIVE_OPERATIONS,
            "draft": op in DRAFT_OPERATIONS,
        }
        for op in AccessOperation
    }
