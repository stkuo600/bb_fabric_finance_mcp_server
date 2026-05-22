"""Database connection management for Microsoft Fabric SQL endpoint."""

from __future__ import annotations

import contextlib
import logging
import struct
import threading

import pyodbc

from src.auth import FabricAuth
from src.models import ColumnInfo, ErrorResponse

logger = logging.getLogger("fabric_mcp.database")

# pyodbc connection attribute for passing access token
_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _build_token_bytes(token: str) -> bytes:
    """Encode an access token for pyodbc's SQL_COPT_SS_ACCESS_TOKEN attribute.

    The token must be encoded as UTF-16LE with a 4-byte length prefix.
    """
    encoded = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)


def _is_connection_error(exc: pyodbc.Error) -> bool:
    """Return True if the pyodbc error is a connection-class failure.

    SQLSTATE class "08" is the ISO SQL "connection exception" class
    (e.g. 08S01 communication link failure, 08001 unable to connect,
    08003 connection not open, 08S02 connection name in use).
    """
    sqlstate = exc.args[0] if exc.args else ""
    return isinstance(sqlstate, str) and sqlstate.startswith("08")


def _hint_for_fabric_error(message: str) -> str | None:
    """Return a Fabric-specific remediation hint for a known error pattern.

    Returns None when the message does not match any known pattern, leaving
    `details` untouched so we never inject misleading guidance.
    """
    lower = message.lower()

    if "identity" in lower and ("overflow" in lower or "arithmetic" in lower):
        return (
            "Fabric Warehouse does not support widening an existing IDENTITY column "
            "via ALTER. Recreate the table with BIGINT IDENTITY and reload the data."
        )

    if (
        ("alter table" in lower and "add" in lower and "column" in lower)
        or ("alter column" in lower and ("not supported" in lower or "unsupported" in lower))
    ):
        return (
            "Fabric Warehouse does not support ALTER TABLE ADD/ALTER COLUMN. "
            "DROP the table and recreate it with the desired schema, then reload the data."
        )

    return None


class FabricDatabase:
    """Manages a long-lived pyodbc connection to a Microsoft Fabric data warehouse.

    A single connection is cached on the instance and reused across calls so the
    TCP + TLS + SQL pre-login + token-auth handshake (typically 500 ms – 2 s
    against a Fabric endpoint) is amortised over many tool invocations. Access
    to the cached connection is serialised by `_lock` — pyodbc connections are
    not safe for concurrent cursor use, and FastMCP's `streamable-http`
    transport can dispatch sync tools to multiple worker threads.
    """

    def __init__(self, server: str, database: str, auth: FabricAuth) -> None:
        self._server = server
        self._database = database
        self._auth = auth
        self._connection_string = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={server},1433;"
            f"DATABASE={database};"
            f"Encrypt=yes;"
            f"TrustServerCertificate=no"
        )
        self._conn: pyodbc.Connection | None = None
        self._lock = threading.Lock()

    def _open_connection(self) -> pyodbc.Connection:
        token = self._auth.get_token()
        token_bytes = _build_token_bytes(token)
        conn = pyodbc.connect(
            self._connection_string,
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_bytes},
            autocommit=True,
        )
        logger.info("Connected to Fabric SQL endpoint", extra={"operation": "connect"})
        return conn

    def _get_connection(self) -> pyodbc.Connection:
        """Return the cached connection, lazily opening it. Must be called with `_lock` held."""
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def _discard_connection(self) -> None:
        """Best-effort close and reset the cached connection. Must be called with `_lock` held."""
        if self._conn is not None:
            with contextlib.suppress(pyodbc.Error):
                self._conn.close()
            self._conn = None

    def execute_query(self, sql: str, timeout: int = 30) -> tuple[list[ColumnInfo], list[dict[str, object]]]:
        """Execute a read-only SQL query and return column metadata and rows.

        Returns (columns, rows) where rows are dicts keyed by column name.
        Raises RuntimeError with ErrorResponse JSON on failure.
        """
        with self._lock:
            for attempt in (0, 1):
                try:
                    conn = self._get_connection()
                    conn.timeout = timeout
                    cursor = conn.cursor()
                    cursor.execute(sql)

                    columns = [
                        ColumnInfo(
                            name=desc[0],
                            type=str(desc[1].__name__) if desc[1] else "unknown",
                            nullable=desc[6] or False,
                        )
                        for desc in cursor.description
                    ]
                    col_names = [c.name for c in columns]
                    rows = [dict(zip(col_names, row, strict=False)) for row in cursor.fetchall()]

                    logger.info(
                        "Query executed: %d rows",
                        len(rows),
                        extra={"operation": "query", "row_count": len(rows)},
                    )
                    return columns, rows
                except pyodbc.Error as e:
                    if attempt == 0 and _is_connection_error(e):
                        logger.warning(
                            "Connection-class error, reconnecting and retrying",
                            extra={"operation": "query", "sqlstate": e.args[0] if e.args else ""},
                        )
                        self._discard_connection()
                        continue
                    message = str(e)
                    error = ErrorResponse(
                        code="QUERY_ERROR",
                        message=message,
                        details=_hint_for_fabric_error(message),
                    )
                    raise RuntimeError(error.model_dump_json()) from e
            raise RuntimeError("unreachable")  # pragma: no cover

    def execute_write(self, sql: str) -> int:
        """Execute a write SQL statement (INSERT/UPDATE) and return affected row count.

        Raises RuntimeError with ErrorResponse JSON on failure.
        """
        with self._lock:
            for attempt in (0, 1):
                try:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(sql)
                    affected = cursor.rowcount
                    logger.info(
                        "Write executed: %d rows affected",
                        affected,
                        extra={"operation": "write", "row_count": affected},
                    )
                    return affected
                except pyodbc.Error as e:
                    if attempt == 0 and _is_connection_error(e):
                        logger.warning(
                            "Connection-class error, reconnecting and retrying",
                            extra={"operation": "write", "sqlstate": e.args[0] if e.args else ""},
                        )
                        self._discard_connection()
                        continue
                    message = str(e)
                    error = ErrorResponse(
                        code="QUERY_ERROR",
                        message=message,
                        details=_hint_for_fabric_error(message),
                    )
                    raise RuntimeError(error.model_dump_json()) from e
            raise RuntimeError("unreachable")  # pragma: no cover
