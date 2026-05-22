"""MCP tools for write operations with two-phase confirmation.

The confirmation token is a stateless, HMAC-signed self-contained string so
that any server replica sharing the same `client_secret` can verify a token
issued by any other replica. There is no server-side token store — see
`.claude/bugfix/2026-05-22-write-token-cross-instance/report.md` for the
design rationale and the deliberate replay-within-window trade-off.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP

from src.config import FabricSettings
from src.database import FabricDatabase
from src.models import ErrorResponse, WritePreview, WriteResult

logger = logging.getLogger("fabric_mcp.tools.write")

_INSERT_PATTERN = re.compile(r"^\s*INSERT\s+INTO\s+(\S+)", re.IGNORECASE)
_UPDATE_PATTERN = re.compile(r"^\s*UPDATE\s+(\S+)", re.IGNORECASE)

_TOKEN_EXPIRY_MINUTES = 5
_TOKEN_VERSION = 1
_SIGNING_KEY_DOMAIN = b"fabric-mcp-write-confirmation\x00"


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


def _signing_key(secret: str) -> bytes:
    return hashlib.sha256(_SIGNING_KEY_DOMAIN + secret.encode("utf-8")).digest()


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _make_token(payload: dict[str, object], secret: str) -> str:
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    sig = hmac.new(_signing_key(secret), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u_encode(sig)}"


def _verify_token(token: str, secret: str) -> dict[str, object] | None:
    """Verify signature and return the decoded payload, or None if invalid."""
    if not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        sig = _b64u_decode(sig_b64)
        expected = hmac.new(_signing_key(secret), payload_b64.encode("ascii"), hashlib.sha256).digest()
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
        return None
    return payload


def register_write_tools(mcp: FastMCP, db: FabricDatabase, config: FabricSettings) -> None:
    """Register write-related MCP tools."""

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

        expires_at = datetime.now(tz=UTC) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)
        payload = {
            "v": _TOKEN_VERSION,
            "sql": sql,
            "op": operation,
            "table": table,
            "exp": expires_at.timestamp(),
            "nonce": secrets.token_hex(16),
        }
        token = _make_token(payload, config.client_secret)

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

        payload = _verify_token(confirmation_token, config.client_secret)
        if payload is None:
            error = ErrorResponse(
                code="TOKEN_INVALID",
                message="Confirmation token is missing, malformed, or has an invalid signature.",
            )
            logger.warning(
                "Invalid token",
                extra={"tool": "fabric_execute_write", "error_code": "TOKEN_INVALID"},
            )
            return error.model_dump_json()

        exp = payload.get("exp")
        if not isinstance(exp, int | float) or exp < datetime.now(tz=UTC).timestamp():
            error = ErrorResponse(
                code="TOKEN_EXPIRED",
                message="Confirmation token has expired. Please preview the write operation again.",
            )
            logger.warning(
                "Expired token",
                extra={"tool": "fabric_execute_write", "error_code": "TOKEN_EXPIRED"},
            )
            return error.model_dump_json()

        sql = str(payload.get("sql", ""))
        operation = str(payload.get("op", ""))
        table = str(payload.get("table", ""))

        try:
            affected_rows = db.execute_write(sql)
        except RuntimeError as e:
            logger.error(
                "Write execution failed",
                extra={"tool": "fabric_execute_write", "error_code": "QUERY_ERROR"},
            )
            return str(e)

        result = WriteResult(
            affected_rows=affected_rows,
            operation=operation,
            table=table,
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
