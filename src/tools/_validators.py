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

import re

from src.tools._responses import ToolInputError

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_BRACKET_IDENT = re.compile(r"\[[^\]]*\]")
_QUOTED_IDENT = re.compile(r'"[^"]*"')


def _strip_sql_literals_and_comments(sql: str) -> str:
    """Blank out SQL comments and string/identifier literals so a subsequent
    structural scan does not see separators or keywords that live inside them.

    Handles: ``--`` line comments, ``/* */`` block comments, ``'...'`` strings
    (with ``''`` escape), ``[...]`` and ``"..."`` quoted identifiers.
    """
    sql = _LINE_COMMENT.sub("", sql)
    sql = _BLOCK_COMMENT.sub("", sql)
    sql = _STRING_LITERAL.sub("''", sql)
    sql = _BRACKET_IDENT.sub("[]", sql)
    sql = _QUOTED_IDENT.sub('""', sql)
    return sql


def validate_single_statement(sql: str) -> str:
    """Return ``sql`` if it is a single statement; raise INVALID_OPERATION otherwise.

    SQL Server / pyodbc executes every ``;``-separated statement in one batch, so
    a leading allowlisted statement followed by a stacked statement (e.g.
    ``INSERT INTO ok ...; UPDATE secret ...``, ``...; DROP TABLE x``) would run
    the trailing statement under the same privileged connection. After stripping
    literals and comments, reject any non-whitespace content following the first
    ``;``. A single trailing ``;`` (and semicolons confined to literals/comments)
    is allowed.
    """
    cleaned = _strip_sql_literals_and_comments(sql)
    _, _, after_first_separator = cleaned.partition(";")
    if after_first_separator.strip():
        raise ToolInputError(
            code="INVALID_OPERATION",
            message=(
                "Only a single SQL statement is allowed; multi-statement batches "
                "(anything after a `;` separator) are rejected."
            ),
        )
    return sql


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
