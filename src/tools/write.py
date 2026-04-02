"""MCP tools for write operations with two-phase confirmation."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from src.config import FabricSettings
from src.database import FabricDatabase
from src.models import ErrorResponse, WritePreview, WriteResult

logger = logging.getLogger("fabric_mcp.tools.write")

_INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\s+(\S+)", re.IGNORECASE)
_UPDATE_PATTERN = re.compile(r"^\s*UPDATE\s+(\S+)", re.IGNORECASE)

_TOKEN_EXPIRY_MINUTES = 5

# In-memory store for pending write confirmations
_pending_writes: dict[str, dict[str, object]] = {}


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


def _is_table_allowed(table: str, allowlist: list[str]) -> bool:
    """Check if a table is on the write allowlist.

    Supports both schema-qualified (gold.table) and unqualified (table) names.
    """
    if not allowlist:
        return False
    table_lower = table.lower().strip("[]\"")
    return any(allowed.lower().strip("[]\"") == table_lower for allowed in allowlist)


def _cleanup_expired_tokens() -> None:
    """Remove expired confirmation tokens."""
    now = datetime.now(tz=UTC)
    expired = [
        token_id
        for token_id, data in _pending_writes.items()
        if data["expires_at"] < now
    ]
    for token_id in expired:
        del _pending_writes[token_id]


def register_write_tools(mcp: FastMCP, db: FabricDatabase, config: FabricSettings) -> None:
    """Register write-related MCP tools."""

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
        _cleanup_expired_tokens()

        parsed = _parse_write_sql(sql)
        if parsed is None:
            error = ErrorResponse(
                code="INVALID_OPERATION",
                message="Only INSERT and UPDATE statements are allowed.",
            )
            logger.warning(
                "Non-write SQL rejected",
                extra={"tool": "fabric_preview_write", "error_code": "INVALID_OPERATION"},
            )
            return error.model_dump_json()

        operation, table = parsed

        if not _is_table_allowed(table, config.write_allowlist):
            allowed_str = ", ".join(config.write_allowlist) if config.write_allowlist else "(none)"
            error = ErrorResponse(
                code="TABLE_NOT_ALLOWED",
                message=f"Table '{table}' is not on the write allowlist",
                details=f"Allowed tables: {allowed_str}",
            )
            logger.warning(
                "Table not allowed: %s",
                table,
                extra={"tool": "fabric_preview_write", "table": table, "error_code": "TABLE_NOT_ALLOWED"},
            )
            return error.model_dump_json()

        token = str(uuid.uuid4())
        expires_at = datetime.now(tz=UTC) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)
        _pending_writes[token] = {
            "sql": sql,
            "operation": operation,
            "table": table,
            "expires_at": expires_at,
        }

        preview = WritePreview(
            confirmation_token=token,
            operation=operation,
            table=table,
            sql_summary=f"{operation} into {table}: {sql[:200]}",
            expires_at=expires_at.isoformat(),
        )
        logger.info(
            "Write preview generated: %s on %s (token=%s)",
            operation,
            table,
            token[:8],
            extra={"tool": "fabric_preview_write", "operation": operation, "table": table},
        )
        return preview.model_dump_json()

    @mcp.tool()
    def fabric_execute_write(confirmation_token: str) -> str:
        """Execute a previously previewed write operation using a confirmation token.

        The token must have been obtained from fabric_preview_write and must not be
        expired (5-minute validity). Each token can only be used once.

        Args:
            confirmation_token: Token from fabric_preview_write.
        """
        logger.info(
            "Write execution requested (token=%s)",
            confirmation_token[:8] if len(confirmation_token) >= 8 else confirmation_token,
            extra={"tool": "fabric_execute_write"},
        )

        # Pop the token first, then check expiry (cleanup runs after to remove other stale tokens)
        pending = _pending_writes.pop(confirmation_token, None)
        _cleanup_expired_tokens()

        if pending is None:
            error = ErrorResponse(
                code="TOKEN_INVALID",
                message="Confirmation token not found or already used.",
            )
            logger.warning(
                "Invalid token",
                extra={"tool": "fabric_execute_write", "error_code": "TOKEN_INVALID"},
            )
            return error.model_dump_json()

        now = datetime.now(tz=UTC)
        if pending["expires_at"] < now:
            error = ErrorResponse(
                code="TOKEN_EXPIRED",
                message="Confirmation token has expired. Please preview the write operation again.",
            )
            logger.warning(
                "Expired token",
                extra={"tool": "fabric_execute_write", "error_code": "TOKEN_EXPIRED"},
            )
            return error.model_dump_json()

        try:
            affected_rows = db.execute_write(str(pending["sql"]))
        except RuntimeError as e:
            logger.error(
                "Write execution failed",
                extra={"tool": "fabric_execute_write", "error_code": "QUERY_ERROR"},
            )
            return str(e)

        result = WriteResult(
            affected_rows=affected_rows,
            operation=str(pending["operation"]),
            table=str(pending["table"]),
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
