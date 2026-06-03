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
import math
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

# Upper bound on SQL byte length accepted by fabric_preview_write. The SQL is
# embedded in the confirmation token (JSON payload + base64 + HMAC sig), so the
# emitted token is roughly 1.4× this size. 60 KB gives ~80 KB tokens, which
# clears the largest transport / proxy size limits we have observed without
# being annoyingly tight for normal multi-row INSERTs. Hitting this returns a
# structured INVALID_OPERATION up-front so the caller does not have to discover
# the limit by suffering an opaque TOKEN_INVALID on the redemption call.
_PREVIEW_SQL_MAX_BYTES = 60_000

# Fixed target table for the bulk Sch1X insert tool. Hard-coded (rather than
# parameterised) because the tool's whole point is to keep the LLM-side payload
# small — letting the LLM choose the table would require it to also generate
# the column list, undoing the gain.
_SCH1X_TABLE = "raw.Fact_Sch1X"

# Per-row metric fields, in the order matching the target table's MTD_* then
# YTD_* columns. The corresponding _Pct columns are emitted as NULL in the
# composed SQL — they are not user-supplied via this tool.
_SCH1X_METRIC_FIELDS = (
    "mtd_actual",
    "mtd_budget",
    "mtd_latest_estimate",
    "mtd_prior_year",
    "ytd_actual",
    "ytd_budget",
    "ytd_latest_estimate",
    "ytd_prior_year",
)

# Full target column list in INSERT order. FactSch1XKey is IDENTITY and is
# omitted so Fabric generates it. LoadedAt receives GETDATE() in every row.
# The eight *_Pct columns are reserved for downstream computation and are
# written as NULL by this tool.
_SCH1X_INSERT_COLUMNS = (
    "EntityKey",
    "PLMappingKey",
    "FiscalYear",
    "FiscalMonth",
    "Version",
    "Unit",
    "MTD_Actual",
    "MTD_Budget",
    "MTD_LatestEstimate",
    "MTD_PriorYear",
    "MTD_Actual_Pct",
    "MTD_Budget_Pct",
    "MTD_LatestEst_Pct",
    "MTD_PriorYear_Pct",
    "YTD_Actual",
    "YTD_Budget",
    "YTD_LatestEstimate",
    "YTD_PriorYear",
    "YTD_Actual_Pct",
    "YTD_Budget_Pct",
    "YTD_LatestEst_Pct",
    "YTD_PriorYear_Pct",
    "LoadedAt",
)


def _fmt_sql_number(v: object) -> str:
    """Render a numeric value as an inline SQL literal.

    Rules:
      * ``None`` → ``NULL``.
      * ``int`` → decimal string (``42`` → ``"42"``).
      * ``float`` whose value is an integer → emitted without the trailing
        ``.0`` (``1000.0`` → ``"1000"``) per spec.
      * ``float`` with a fractional part → ``repr(v)``, which for normal
        finance-sized numbers stays in plain decimal notation (no scientific).
      * Other types raise ``ValueError``. ``bool`` is rejected explicitly
        even though it is an ``int`` subclass — silently coercing ``True``
        to ``1`` for a metric column would mask an LLM-side type bug.
      * NaN / ±inf raise ``ValueError`` since SQL Server has no literal for
        them and the resulting cast error would be opaque to the caller.
    """
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        msg = f"boolean is not a valid numeric value: {v!r}"
        raise ValueError(msg)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            msg = f"non-finite float is not allowed: {v!r}"
            raise ValueError(msg)
        if v == int(v):
            return str(int(v))
        return repr(v)
    msg = f"unsupported numeric type {type(v).__name__}: {v!r}"
    raise ValueError(msg)


def _escape_sql_string(s: str) -> str:
    """Escape a string for use as a single-quoted SQL Server literal.

    Doubles embedded single quotes (the only T-SQL escape needed inside
    ``'...'``). The caller is responsible for wrapping the result in
    single quotes.
    """
    return s.replace("'", "''")


def _token_signing_key(config: FabricSettings) -> str:
    """Resolve the HMAC signing material for confirmation tokens.

    Prefer the dedicated, rotation-stable ``FABRIC_TOKEN_SIGNING_KEY``. Fall
    back to ``client_secret`` only for backward compatibility — but warn,
    because the AAD ``client_secret`` is rotated periodically on ACA and any
    rotation would invalidate all in-flight tokens and break cross-replica
    verification (see .claude/bugfix/2026-06-03-token-signing-key-rotation).
    """
    if config.token_signing_key:
        return config.token_signing_key
    logger.warning(
        "FABRIC_TOKEN_SIGNING_KEY is not set; confirmation tokens are signed with "
        "client_secret and will be invalidated when it rotates. Set a dedicated "
        "FABRIC_TOKEN_SIGNING_KEY so tokens survive AAD-secret rotation.",
        extra={"tool": "fabric_write"},
    )
    return config.client_secret


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

        # Two-phase: HMAC verification + expiry check is CPU-bound and
        # independent per-token, so it runs lock-free. Only the DB-bound
        # statements that actually need execution are passed to
        # `execute_writes`, which acquires the connection lock once and
        # reuses a single cursor — amortising lock / cursor / retry-stack
        # overhead across the batch.
        results: list[dict[str, object] | None] = [None] * len(confirmation_tokens)
        pending: list[tuple[int, object]] = []  # (original_index, payload)
        now_ts = datetime.now(tz=UTC).timestamp()

        for i, token in enumerate(confirmation_tokens):
            try:
                payload = parse_confirmation_token(token, _token_signing_key(config))
            except ToolInputError as e:
                results[i] = {"status": "error", "code": e.code, "message": e.message}
                continue

            try:
                ensure_token_not_expired(payload, now=now_ts)
            except ToolInputError as e:
                results[i] = {
                    "status": "error",
                    "code": e.code,
                    "message": e.message,
                    "operation": payload.op,
                    "table": payload.table,
                }
                continue

            pending.append((i, payload))

        if pending:
            outcomes = db.execute_writes([p.sql for _, p in pending])
            for (i, payload), outcome in zip(pending, outcomes, strict=True):
                if isinstance(outcome, FabricQueryError):
                    results[i] = {
                        "status": "error",
                        "operation": payload.op,
                        "table": payload.table,
                        "code": outcome.code,
                        "message": outcome.message,
                        "details": outcome.details,
                    }
                else:
                    results[i] = {
                        "status": "ok",
                        "affected_rows": outcome,
                        "operation": payload.op,
                        "table": payload.table,
                    }
                    logger.info(
                        "Batch write executed: %s on %s, %d rows affected",
                        payload.op,
                        payload.table,
                        outcome,
                        extra={
                            "tool": "fabric_execute_write_batch",
                            "operation": payload.op,
                            "table": payload.table,
                            "row_count": outcome,
                        },
                    )

        succeeded = sum(1 for r in results if r is not None and r.get("status") == "ok")
        failed = sum(1 for r in results if r is not None and r.get("status") == "error")

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
            sql_bytes = len(sql.encode("utf-8"))
            if sql_bytes > _PREVIEW_SQL_MAX_BYTES:
                raise ToolInputError(
                    code="INVALID_OPERATION",
                    message=(
                        f"SQL is too large ({sql_bytes} bytes; max "
                        f"{_PREVIEW_SQL_MAX_BYTES}). The SQL is embedded in the "
                        "confirmation token, so an oversize SQL produces a token "
                        "the transport layer may reject as TOKEN_INVALID. Split "
                        "the statement into smaller batches and retry."
                    ),
                )
        except ToolInputError as e:
            return error_envelope(e, tool="fabric_preview_write")

        token, expires_at = make_confirmation_token(
            sql=sql,
            op=operation,
            table=table,
            secret=_token_signing_key(config),
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
            payload = parse_confirmation_token(confirmation_token, _token_signing_key(config))
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

    @mcp.tool()
    def fabric_insert_sch1x_rows(
        entity_key: int,
        fiscal_year: int,
        fiscal_month: int,
        version: int,
        unit: str,
        rows: list[dict],
    ) -> str:
        """Bulk-insert structured rows into raw.Fact_Sch1X without a SQL string.

        PREFERRED for bulk INSERT into raw.Fact_Sch1X. Accepts structured row
        data; the server composes the multi-row INSERT and executes it in a
        single round-trip. Avoids the slow path of fabric_preview_write +
        fabric_execute_write, where the LLM has to emit the multi-row VALUES
        SQL text token-by-token (the dominant wall-time cost for batches
        above a handful of rows).

        The target table and column list are fixed: EntityKey / FiscalYear /
        FiscalMonth / Version / Unit identify the slice and apply to every
        row; PLMappingKey and the eight MTD_* / YTD_* metric values come
        from each entry in `rows`. The eight *_Pct columns are written as
        NULL (this tool does not compute them). LoadedAt is set to
        GETDATE(). FactSch1XKey is IDENTITY and is generated by Fabric.

        Args:
            entity_key: EntityKey applied to every inserted row.
            fiscal_year: FiscalYear (1900-9999).
            fiscal_month: FiscalMonth (1-12).
            version: Version applied to every row.
            unit: Unit string applied to every row (e.g. "1000").
            rows: Non-empty list of dicts. Each dict must contain
                ``pl_mapping_key`` (int) plus the eight metric values
                ``mtd_actual`` / ``mtd_budget`` / ``mtd_latest_estimate`` /
                ``mtd_prior_year`` / ``ytd_actual`` / ``ytd_budget`` /
                ``ytd_latest_estimate`` / ``ytd_prior_year`` (float or
                None). Missing metric keys are treated as None (NULL).

        Returns:
            On success, JSON: ``{rows_inserted, entity_key, fiscal_year,
            fiscal_month, version}``. On rejection or DB failure: structured
            ``INVALID_OPERATION`` / ``TABLE_NOT_ALLOWED`` / ``QUERY_ERROR``
            envelope identifying the offending row index where applicable.
        """
        logger.info(
            "Sch1X bulk insert requested",
            extra={
                "tool": "fabric_insert_sch1x_rows",
                "entity_key": entity_key,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "version": version,
                "row_count": len(rows),
            },
        )

        try:
            if not rows:
                raise ToolInputError(
                    code="INVALID_OPERATION",
                    message="rows must be non-empty; no statement was executed.",
                )

            # The hard-coded target table must still be on the operator-
            # controlled allowlist; this keeps a single env-var the way to
            # gate any write tool, including this one.
            validate_writable_table(_SCH1X_TABLE, allowlist=config.write_allowlist)
            validate_int_range(fiscal_year, name="fiscal_year", lo=1900, hi=9999)
            validate_int_range(fiscal_month, name="fiscal_month", lo=1, hi=12)

            unit_lit = f"'{_escape_sql_string(unit)}'"
            ek_lit = str(int(entity_key))
            fy_lit = str(int(fiscal_year))
            fm_lit = str(int(fiscal_month))
            ver_lit = str(int(version))

            values_rows: list[str] = []
            for i, row in enumerate(rows):
                pl_raw = row.get("pl_mapping_key")
                if pl_raw is None or isinstance(pl_raw, bool):
                    raise ToolInputError(
                        code="INVALID_OPERATION",
                        message=(
                            f"rows[{i}].pl_mapping_key is missing or invalid: "
                            f"{pl_raw!r}"
                        ),
                    )
                try:
                    pl_key = int(pl_raw)
                except (TypeError, ValueError) as e:
                    raise ToolInputError(
                        code="INVALID_OPERATION",
                        message=(
                            f"rows[{i}].pl_mapping_key is not an int: {pl_raw!r}"
                        ),
                    ) from e

                metric_literals: list[str] = []
                for field in _SCH1X_METRIC_FIELDS:
                    try:
                        metric_literals.append(_fmt_sql_number(row.get(field)))
                    except ValueError as ve:
                        raise ToolInputError(
                            code="INVALID_OPERATION",
                            message=f"rows[{i}].{field}: {ve}",
                        ) from ve

                mtd_vals = metric_literals[:4]
                ytd_vals = metric_literals[4:]

                values_rows.append(
                    f"({ek_lit}, {pl_key}, {fy_lit}, {fm_lit}, {ver_lit}, "
                    f"{unit_lit}, "
                    f"{mtd_vals[0]}, {mtd_vals[1]}, {mtd_vals[2]}, {mtd_vals[3]}, "
                    f"NULL, NULL, NULL, NULL, "
                    f"{ytd_vals[0]}, {ytd_vals[1]}, {ytd_vals[2]}, {ytd_vals[3]}, "
                    f"NULL, NULL, NULL, NULL, "
                    f"GETDATE())"
                )

            sql = (
                f"INSERT INTO {_SCH1X_TABLE} ("
                + ", ".join(_SCH1X_INSERT_COLUMNS)
                + ") VALUES "
                + ", ".join(values_rows)
            )

            affected = db.execute_write(sql)
        except (ToolInputError, FabricQueryError) as e:
            return error_envelope(e, tool="fabric_insert_sch1x_rows")

        result = {
            "rows_inserted": affected,
            "entity_key": entity_key,
            "fiscal_year": fiscal_year,
            "fiscal_month": fiscal_month,
            "version": version,
        }
        logger.info(
            "Sch1X bulk insert completed: %d rows",
            affected,
            extra={
                "tool": "fabric_insert_sch1x_rows",
                "entity_key": entity_key,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "version": version,
                "row_count": affected,
            },
        )
        return json.dumps(result)
