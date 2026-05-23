"""MCP tools for write operations with two-phase confirmation.

The confirmation token is a stateless, HMAC-signed self-contained string so
that any server replica sharing the same `client_secret` can verify a token
issued by any other replica. There is no server-side token store — see
`.claude/bugfix/2026-05-22-write-token-cross-instance/report.md` for the
design rationale and the deliberate replay-within-window trade-off.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from src.config import FabricSettings
from src.database import FabricDatabase, FabricQueryError
from src.models import WritePreview, WriteResult
from src.tools._confirmation_token import (
    ensure_token_not_expired,
    make_confirmation_token,
    parse_confirmation_token,
)
from src.tools._responses import ToolInputError, error_envelope
from src.tools._validators import validate_int_range, validate_writable_table

logger = logging.getLogger("fabric_mcp.tools.write")

_INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\s+(\S+)", re.IGNORECASE)
_UPDATE_PATTERN = re.compile(r"^\s*UPDATE\s+(\S+)", re.IGNORECASE)

_BATCH_MAX_TOKENS = 100


def _parse_write_sql(sql: str) -> tuple[str, str] | None:
    """Extract operation type and target table from INSERT/UPDATE SQL.

    Returns (operation, table) or None if not a valid write statement.
    """
    match = _INSERT_PATTERN.match(sql)
    if match:
        return "INSERT", match.group(1)
    match = _UPDATE_PATTERN.match(sql)
    if match:
        return "UPDATE", match.group(1)
    return None


def register_write_tools(mcp: FastMCP, db: FabricDatabase, config: FabricSettings) -> None:
    """Register write-related MCP tools."""

    @mcp.tool()
    def fabric_execute_write_batch(confirmation_tokens: list[str]) -> str:
        """Redeem multiple confirmation tokens in one round-trip.

        Best-effort semantics: every token is verified and executed
        independently. A failure on one token does not roll back earlier
        successes nor stop later tokens from executing. The response
        carries per-token status plus succeeded/failed counts, so the
        caller can detect partial failure and act on it.

        Args:
            confirmation_tokens: List of tokens previously issued by
                fabric_preview_write. Max 100 per call; a longer list is
                refused with INVALID_OPERATION before any verification.

        Returns:
            {
              "results": [
                {"status": "ok", "affected_rows": N, "operation": "INSERT", "table": "..."},
                {"status": "error", "code": "TOKEN_INVALID", "operation": "...", "table": "...", ...},
                ...
              ],
              "total_succeeded": <int>,
              "total_failed": <int>
            }
        """
        try:
            validate_int_range(
                len(confirmation_tokens),
                name="batch size",
                lo=0,
                hi=_BATCH_MAX_TOKENS,
            )
        except ToolInputError as e:
            return error_envelope(e, tool="fabric_execute_write_batch")

        logger.info(
            "Write batch requested: %d tokens",
            len(confirmation_tokens),
            extra={"tool": "fabric_execute_write_batch", "count": len(confirmation_tokens)},
        )

        results: list[dict[str, object]] = []
        succeeded = 0
        failed = 0
        now_ts = datetime.now(tz=UTC).timestamp()

        for token in confirmation_tokens:
            # Two try blocks: TOKEN_INVALID has no payload to surface,
            # but TOKEN_EXPIRED / FabricQueryError must preserve op/table
            # for the per-token UX.
            try:
                payload = parse_confirmation_token(token, config.client_secret)
            except ToolInputError as e:
                results.append({"status": "error", "code": e.code, "message": e.message})
                failed += 1
                continue

            try:
                ensure_token_not_expired(payload, now=now_ts)
                affected = db.execute_write(payload.sql)
            except ToolInputError as e:
                results.append(
                    {
                        "status": "error",
                        "code": e.code,
                        "message": e.message,
                        "operation": payload.op,
                        "table": payload.table,
                    }
                )
                failed += 1
                continue
            except FabricQueryError as e:
                results.append(
                    {
                        "status": "error",
                        "operation": payload.op,
                        "table": payload.table,
                        "code": e.code,
                        "message": e.message,
                        "details": e.details,
                    }
                )
                failed += 1
                continue

            results.append(
                {
                    "status": "ok",
                    "affected_rows": affected,
                    "operation": payload.op,
                    "table": payload.table,
                }
            )
            succeeded += 1
            logger.info(
                "Batch write executed: %s on %s, %d rows affected",
                payload.op,
                payload.table,
                affected,
                extra={
                    "tool": "fabric_execute_write_batch",
                    "operation": payload.op,
                    "table": payload.table,
                    "row_count": affected,
                },
            )

        logger.info(
            "Write batch completed: %d succeeded, %d failed",
            succeeded,
            failed,
            extra={
                "tool": "fabric_execute_write_batch",
                "total_succeeded": succeeded,
                "total_failed": failed,
            },
        )
        return json.dumps(
            {
                "results": results,
                "total_succeeded": succeeded,
                "total_failed": failed,
            }
        )

    @mcp.tool()
    def fabric_delete_period(table: str, fiscal_year: int, fiscal_month: int) -> str:
        """Delete one fiscal period's rows from an allowlisted fact table.

        The WHERE clause is fixed to `FiscalYear = ? AND FiscalMonth = ?`;
        arbitrary DELETE is not supported. Intended for monthly fact-table
        reload workflows (e.g. FX rate re-import).

        Args:
            table: Schema-qualified target (e.g. "raw.Fact_ExchangeRate").
                Must be on the FABRIC_WRITE_ALLOWLIST **and** have both
                `FiscalYear` and `FiscalMonth` columns.
            fiscal_year: Four-digit fiscal year (1900-9999).
            fiscal_month: Fiscal month (1-12).

        Returns:
            On success: `{"deleted_rows": N, "table": ..., "fiscal_year": ..., "fiscal_month": ...}`.
            On rejection: `INVALID_OPERATION` (bad args / unqualified table /
            missing FiscalYear or FiscalMonth columns) or `TABLE_NOT_ALLOWED`.
        """
        logger.info(
            "Delete period requested",
            extra={
                "tool": "fabric_delete_period",
                "table": table,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
            },
        )

        try:
            validate_int_range(fiscal_year, name="fiscal_year", lo=1900, hi=9999)
            validate_int_range(fiscal_month, name="fiscal_month", lo=1, hi=12)
            validate_writable_table(
                table,
                allowlist=config.write_allowlist,
                require_qualified=True,
            )

            schema_name, table_name = table.split(".", 1)
            check_sql = (
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_SCHEMA = '{schema_name}' AND TABLE_NAME = '{table_name}' "
                "AND COLUMN_NAME IN ('FiscalYear', 'FiscalMonth')"
            )
            _, col_rows = db.execute_query(check_sql)

            found_lower = {str(row["COLUMN_NAME"]).lower() for row in col_rows}
            required = ("FiscalYear", "FiscalMonth")
            missing = [c for c in required if c.lower() not in found_lower]
            if missing:
                raise ToolInputError(
                    code="INVALID_OPERATION",
                    message=(
                        f"Table '{table}' is missing required column(s) {missing}; "
                        "fabric_delete_period only supports tables with both FiscalYear and FiscalMonth."
                    ),
                )

            delete_sql = (
                f"DELETE FROM {table} WHERE FiscalYear = {fiscal_year} AND FiscalMonth = {fiscal_month}"
            )
            deleted_rows = db.execute_write(delete_sql)
        except (ToolInputError, FabricQueryError) as e:
            return error_envelope(e, tool="fabric_delete_period")

        result = {
            "deleted_rows": deleted_rows,
            "table": table,
            "fiscal_year": fiscal_year,
            "fiscal_month": fiscal_month,
        }
        logger.info(
            "Delete period executed: %s year=%d month=%d rows=%d",
            table,
            fiscal_year,
            fiscal_month,
            deleted_rows,
            extra={
                "tool": "fabric_delete_period",
                "table": table,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "row_count": deleted_rows,
            },
        )
        return json.dumps(result)

    @mcp.tool()
    def fabric_list_writable_tables() -> str:
        """List tables on the write allowlist.

        Returns the set of tables that can be the target of
        `fabric_preview_write` / `fabric_execute_write`. Reads the
        server's configured `write_allowlist` (FABRIC_WRITE_ALLOWLIST
        env var) — no database round-trip.
        """
        logger.info(
            "Writable tables requested",
            extra={"tool": "fabric_list_writable_tables", "count": len(config.write_allowlist)},
        )
        return json.dumps({"writable_tables": list(config.write_allowlist)})

    @mcp.tool()
    def fabric_preview_write(sql: str) -> str:
        """Preview a write operation and receive a confirmation token. Does NOT execute the SQL.

        Only INSERT and UPDATE statements are accepted. The target table must be on the
        configured write allowlist. Returns a confirmation token that must be passed to
        fabric_execute_write to actually execute the operation.

        Args:
            sql: SQL INSERT or UPDATE statement to preview.
        """
        logger.info("Write preview requested", extra={"tool": "fabric_preview_write"})

        try:
            parsed = _parse_write_sql(sql)
            if parsed is None:
                raise ToolInputError(
                    code="INVALID_OPERATION",
                    message="Only INSERT and UPDATE statements are allowed.",
                )
            operation, table = parsed
            validate_writable_table(table, allowlist=config.write_allowlist)
        except ToolInputError as e:
            return error_envelope(e, tool="fabric_preview_write")

        token, expires_at = make_confirmation_token(
            sql=sql,
            op=operation,
            table=table,
            secret=config.client_secret,
            expires_in=timedelta(minutes=config.write_token_expiry_minutes),
        )

        preview = WritePreview(
            confirmation_token=token,
            operation=operation,
            table=table,
            sql_summary=f"{operation} into {table}: {sql[:200]}",
            expires_at=expires_at.isoformat(),
        )
        logger.info(
            "Write preview generated: %s on %s",
            operation,
            table,
            extra={"tool": "fabric_preview_write", "operation": operation, "table": table},
        )
        return preview.model_dump_json()

    @mcp.tool()
    def fabric_execute_write(confirmation_token: str) -> str:
        """Execute a previously previewed write operation using a confirmation token.

        The token must have been obtained from fabric_preview_write and must not be
        expired (5-minute validity).

        Args:
            confirmation_token: Token from fabric_preview_write.
        """
        logger.info(
            "Write execution requested",
            extra={"tool": "fabric_execute_write"},
        )

        try:
            payload = parse_confirmation_token(confirmation_token, config.client_secret)
            ensure_token_not_expired(payload, now=datetime.now(tz=UTC).timestamp())
            affected_rows = db.execute_write(payload.sql)
        except (ToolInputError, FabricQueryError) as e:
            return error_envelope(e, tool="fabric_execute_write")

        result = WriteResult(
            affected_rows=affected_rows,
            operation=payload.op,
            table=payload.table,
        )
        logger.info(
            "Write executed: %s on %s, %d rows affected",
            result.operation,
            result.table,
            result.affected_rows,
            extra={
                "tool": "fabric_execute_write",
                "operation": result.operation,
                "table": result.table,
                "row_count": result.affected_rows,
            },
        )
        return result.model_dump_json()
