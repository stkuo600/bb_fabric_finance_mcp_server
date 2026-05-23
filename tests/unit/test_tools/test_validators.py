"""Unit tests for the shared tool-input validators + response helpers.

These cover the seam used by every MCP tool to (a) reject bad input with a
structured exception, (b) convert any thrown error — input or downstream
FabricQueryError — to the on-wire ErrorResponse JSON envelope.
"""

from __future__ import annotations

import json

import pytest

from src.database import FabricQueryError
from src.tools._responses import ToolInputError, error_envelope
from src.tools._validators import validate_int_range, validate_writable_table


class TestToolInputError:
    """ToolInputError carries the same shape as FabricQueryError so both
    can be caught + serialised through one envelope helper."""

    def test_carries_code_message_details(self) -> None:
        e = ToolInputError(code="INVALID_OPERATION", message="bad arg", details="hint")
        assert e.code == "INVALID_OPERATION"
        assert e.message == "bad arg"
        assert e.details == "hint"

    def test_details_defaults_to_none(self) -> None:
        e = ToolInputError(code="X", message="m")
        assert e.details is None

    def test_is_an_exception(self) -> None:
        with pytest.raises(ToolInputError):
            raise ToolInputError(code="X", message="m")


class TestErrorEnvelope:
    """error_envelope is the single seam that turns any
    ToolInputError or FabricQueryError into the on-wire JSON envelope.
    The client-facing shape is `{code, message, details}` — unchanged
    from pre-refactor behaviour."""

    def test_envelope_of_tool_input_error(self) -> None:
        e = ToolInputError(code="INVALID_OPERATION", message="bad", details="hint")
        out = json.loads(error_envelope(e, tool="some_tool"))
        assert out == {"code": "INVALID_OPERATION", "message": "bad", "details": "hint"}

    def test_envelope_of_fabric_query_error(self) -> None:
        e = FabricQueryError(
            message="syntax error",
            code="QUERY_ERROR",
            details=None,
            sqlstate="42000",
        )
        out = json.loads(error_envelope(e, tool="some_tool"))
        assert out == {"code": "QUERY_ERROR", "message": "syntax error", "details": None}
        # sqlstate is intentionally not exposed on the client envelope —
        # it's a server-internal diagnostic.
        assert "sqlstate" not in out

    def test_envelope_omits_no_fields(self) -> None:
        """ErrorResponse keys must all be present even when details is None,
        so clients can rely on key shape, not key existence."""
        e = ToolInputError(code="X", message="m")
        out = json.loads(error_envelope(e, tool="t"))
        assert set(out.keys()) == {"code", "message", "details"}


class TestValidateIntRange:
    """A generic int-range guard. Replaces hand-rolled range checks across
    fiscal_year (1900-9999), fiscal_month (1-12), max_rows (1-10000),
    and batch size."""

    def test_value_within_range_returns_value(self) -> None:
        assert validate_int_range(5, name="x", lo=1, hi=10) == 5

    def test_value_at_low_boundary_ok(self) -> None:
        assert validate_int_range(1, name="x", lo=1, hi=10) == 1

    def test_value_at_high_boundary_ok(self) -> None:
        assert validate_int_range(10, name="x", lo=1, hi=10) == 10

    def test_below_low_raises_with_name_and_value_in_message(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            validate_int_range(0, name="fiscal_month", lo=1, hi=12)
        e = exc_info.value
        assert e.code == "INVALID_OPERATION"
        assert "fiscal_month" in e.message
        assert "0" in e.message
        assert "1" in e.message and "12" in e.message  # bounds disclosed

    def test_above_high_raises(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            validate_int_range(13, name="fiscal_month", lo=1, hi=12)
        assert "fiscal_month" in exc_info.value.message
        assert "13" in exc_info.value.message


class TestValidateWritableTable:
    """Single source of truth for the write-allowlist check (previously
    duplicated in fabric_preview_write and fabric_delete_period). The
    `require_qualified` knob makes the schema-qualified rule explicit
    rather than the prior silent inconsistency."""

    def test_allowlisted_returns_table(self) -> None:
        assert validate_writable_table(
            "gold.transactions",
            allowlist=["gold.transactions", "gold.audit"],
        ) == "gold.transactions"

    def test_not_on_allowlist_raises_table_not_allowed(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            validate_writable_table(
                "raw.secret",
                allowlist=["gold.transactions"],
            )
        e = exc_info.value
        assert e.code == "TABLE_NOT_ALLOWED"
        assert "raw.secret" in e.message
        # The details field discloses the actual allowlist so the LLM
        # caller can self-correct rather than guess.
        assert e.details is not None
        assert "gold.transactions" in e.details

    def test_empty_allowlist_rejects_everything(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            validate_writable_table("any.table", allowlist=[])
        assert exc_info.value.code == "TABLE_NOT_ALLOWED"

    def test_case_insensitive_match(self) -> None:
        """Existing behaviour: _is_table_allowed lowercases both sides
        before comparing. Preserve that."""
        assert validate_writable_table(
            "GOLD.Transactions",
            allowlist=["gold.transactions"],
        ) == "GOLD.Transactions"

    def test_brackets_and_quotes_stripped_before_compare(self) -> None:
        """Existing behaviour: [gold.transactions] and "gold.transactions"
        match the allowlisted form."""
        assert validate_writable_table(
            "[gold.transactions]",
            allowlist=["gold.transactions"],
        ) == "[gold.transactions]"

    def test_require_qualified_rejects_unqualified(self) -> None:
        """fabric_delete_period needs to split schema.table to query
        INFORMATION_SCHEMA; an unqualified name would crash that
        downstream. Reject up-front with INVALID_OPERATION (not
        TABLE_NOT_ALLOWED — wrong rule)."""
        with pytest.raises(ToolInputError) as exc_info:
            validate_writable_table(
                "naked_name",
                allowlist=["naked_name"],
                require_qualified=True,
            )
        e = exc_info.value
        assert e.code == "INVALID_OPERATION"
        assert "schema-qualified" in e.message.lower() or "schema" in e.message.lower()

    def test_require_qualified_accepts_qualified(self) -> None:
        assert validate_writable_table(
            "raw.Fact_ExchangeRate",
            allowlist=["raw.Fact_ExchangeRate"],
            require_qualified=True,
        ) == "raw.Fact_ExchangeRate"

    def test_require_qualified_default_false_preserves_old_behaviour(self) -> None:
        """fabric_preview_write currently accepts unqualified names if
        they happen to be on the allowlist. Default must preserve that —
        changing it would be a product decision, not a refactor."""
        assert validate_writable_table(
            "naked",
            allowlist=["naked"],
        ) == "naked"
