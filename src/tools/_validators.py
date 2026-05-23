"""Reusable input validators for MCP tools.

Each validator raises ``ToolInputError`` on rejection and returns the
validated value on success — so callers compose them in a single
``try`` block that handles all input failures uniformly via
``error_envelope`` in ``_responses``.

Validators here are pure (no I/O, no config); checks that need
``FabricDatabase`` or ``FabricSettings`` stay inline at the tool — keeping
this module testable without mocks.
"""

from __future__ import annotations

from src.tools._responses import ToolInputError


def validate_int_range(value: int, *, name: str, lo: int, hi: int) -> int:
    """Return ``value`` if ``lo <= value <= hi``; raise INVALID_OPERATION otherwise.

    The error message names the parameter and discloses both the rejected
    value and the accepted bounds so the LLM caller can self-correct.
    """
    if not lo <= value <= hi:
        raise ToolInputError(
            code="INVALID_OPERATION",
            message=f"{name} must be between {lo} and {hi}; got {value}.",
        )
    return value


def _normalise_table(table: str) -> str:
    """Lower-case + strip surrounding brackets / quotes for allowlist match."""
    return table.lower().strip("[]\"")


def validate_writable_table(
    table: str,
    *,
    allowlist: list[str],
    require_qualified: bool = False,
) -> str:
    """Return ``table`` if it passes both checks; raise otherwise.

    Two independent checks:

    1. ``table`` must be on ``allowlist`` (case-insensitive,
       brackets/quotes stripped). Failure → TABLE_NOT_ALLOWED with the
       allowlist disclosed in ``details``.
    2. If ``require_qualified=True``, ``table`` must contain ``.`` so it
       can be split into schema + name downstream. Failure →
       INVALID_OPERATION.

    The default ``require_qualified=False`` preserves the pre-refactor
    behaviour of ``fabric_preview_write`` (which accepted unqualified
    names on the allowlist); ``fabric_delete_period`` opts in to the
    stricter rule.
    """
    if require_qualified and "." not in table:
        raise ToolInputError(
            code="INVALID_OPERATION",
            message=(
                f"table must be schema-qualified (e.g. 'raw.Fact_ExchangeRate'); "
                f"got '{table}'."
            ),
        )

    table_norm = _normalise_table(table)
    if not allowlist or not any(_normalise_table(a) == table_norm for a in allowlist):
        allowed_str = ", ".join(allowlist) if allowlist else "(none)"
        raise ToolInputError(
            code="TABLE_NOT_ALLOWED",
            message=f"Table '{table}' is not on the write allowlist",
            details=f"Allowed tables: {allowed_str}",
        )
    return table
