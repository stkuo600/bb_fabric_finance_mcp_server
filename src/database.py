"""Database connection management for Microsoft Fabric SQL endpoint."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import pyodbc

from src.auth import FabricAuth
from src.models import ColumnInfo

logger = logging.getLogger("fabric_mcp.database")

_T = TypeVar("_T")

# Per-fetch batch size handed to the ODBC driver via cursor.arraysize and the
# corresponding cursor.fetchmany(N) loop in execute_query. Without this, pyodbc
# fetches rows from the ODBC driver one at a time (SQLFetch per row), which
# dominates wall time on Fabric round-trips. pyodbc's docs only document
# arraysize as fetchmany's default size — fetchall() does NOT honor it — so the
# loop is required, not optional, to realise the batching gain. 1000 is a
# pragmatic ceiling: covers the per-call max_rows cap (10000) in ≤10 chunks
# without bloating the driver's row buffer.
_FETCH_BATCH_SIZE = 1000


def _sql_hash(sql: str) -> str:
    """Stable 16-hex-char fingerprint of a SQL string for log correlation.

    Short enough for log readability; long enough to disambiguate the
    queries an operator is likely to be looking at simultaneously. Not
    a security boundary — full SHA-256 truncated to 64 bits.
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


@dataclass
class FabricQueryError(Exception):
    """Structured failure surfaced by ``FabricDatabase.execute_query`` /
    ``execute_write``. Carries the same fields callers used to recover by
    JSON-parsing the prior ``RuntimeError`` message, plus the originating
    SQLSTATE for callers that want to dispatch on connection vs query-side
    errors without re-classifying the underlying pyodbc.Error.
    """

    message: str
    code: str = "QUERY_ERROR"
    details: str | None = None
    sqlstate: str | None = None

    def __str__(self) -> str:  # pragma: no cover - debug aid only
        return f"[{self.code}] {self.message}"

# pyodbc connection attribute for passing access token
_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _build_token_bytes(token: str) -> bytes:
    """Encode an access token for pyodbc's SQL_COPT_SS_ACCESS_TOKEN attribute.

    The token must be encoded as UTF-16LE with a 4-byte length prefix.
    """
    encoded = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)


_CONNECTION_SQLSTATE_PREFIXES = ("08", "IMC", "HYT")

# Fragments of MS-ODBC English diagnostic strings that signal a dead/suspect
# connection even when wrapped in a generic SQLSTATE (most commonly HY000).
# Kept narrow to avoid misclassifying genuine query-side HY000 errors as
# connection failures.
_CONNECTION_MESSAGE_FRAGMENTS = (
    "communication link",
    "tcp provider",
    "connection is broken",
    "connection forcibly closed",
    "server has terminated the connection",
)


def _is_connection_error(exc: pyodbc.Error) -> bool:
    """Return True if the pyodbc error signals a dead/suspect connection.

    Matches SQLSTATEs the Microsoft ODBC driver actually emits on idle
    disconnect and broken-connection scenarios against Fabric / Azure SQL:

    - ``08*`` — ISO SQL connection-exception class (e.g. ``08S01``
      communication link failure, ``08001`` unable to connect).
    - ``IMC*`` — Microsoft connection-resiliency states (``IMC01`` recovery
      failed, ``IMC05`` server-marked unrecoverable, ``IMC06`` client-driver
      marked unrecoverable — once set, the driver refuses to attempt
      recovery, so every subsequent query returns ``IMC06`` until the
      connection is rebuilt).
    - ``HYT*`` — connection / query timeout (``HYT00``, ``HYT01``).

    Falls back to a narrow substring scan of the error message for cases
    where the SQLSTATE is generic (``HY000``) but the driver-supplied text
    contains a known connection-failure phrase. Microsoft Learn:
    *Connection resiliency in the ODBC driver*.
    """
    sqlstate = exc.args[0] if exc.args else ""
    if isinstance(sqlstate, str) and sqlstate.startswith(_CONNECTION_SQLSTATE_PREFIXES):
        return True
    text = str(exc).lower()
    return any(fragment in text for fragment in _CONNECTION_MESSAGE_FRAGMENTS)


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
            f"TrustServerCertificate=no;"
            # Driver-level idle connection resiliency. Up to 3 silent
            # reconnect attempts (10s apart) before surfacing an error —
            # handles the common Fabric idle-disconnect case transparently.
            # Supported on Fabric SQL database per MS Learn "Connection
            # resiliency in the ODBC driver".
            f"ConnectRetryCount=3;ConnectRetryInterval=10;"
            # TCP keep-alive: send a probe after 10s of socket idleness.
            # Driver default is 30s, which is too late for heavy Fabric
            # views whose silent-compute window (~20s observed) gets the
            # connection killed by an Azure-side intermediary before any
            # probe fires. See .claude/bugfix/2026-05-23-fabric-08s01-
            # mid-query-tcp-drop. KeepAliveInterval=1s = probe retransmit
            # cadence if no ack received.
            f"KeepAlive=10;KeepAliveInterval=1;"
            # Surfaces in Fabric queryinsights.exec_requests_history.
            # program_name so DBAs can filter requests originating from
            # this MCP server in server-side telemetry.
            f"APP=fabric-mcp"
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

    def _redact_identifiers(self, text: str) -> str:
        """Remove backend identifiers from a client-facing error message.

        ODBC/Fabric diagnostics embed the Fabric server FQDN and database name;
        returning them verbatim across the MCP trust boundary is reconnaissance-
        grade information disclosure. Replace those identifiers with
        ``<redacted>`` while preserving useful query-side text (e.g. "Invalid
        object name") that helps the caller self-correct. The full raw text is
        logged server-side for diagnosis.
        """
        redacted = text
        for ident in (self._server, self._database):
            if ident:
                redacted = redacted.replace(ident, "<redacted>")
        return redacted

    def _execute_with_retry(
        self,
        operation: Callable[[pyodbc.Connection], tuple[_T, int]],
        *,
        op_label: str,
        sql_for_hash: str,
        retry_on_connection_error: bool = True,
    ) -> _T:
        """Run ``operation`` against the cached connection with one immediate
        reconnect-and-retry on connection-class pyodbc errors.

        ``operation`` receives the live ``pyodbc.Connection`` and must
        return ``(result, row_count)``. The helper owns timing + retry +
        logging so every attempt — success, retry, or final failure —
        is observable through a uniform set of structured fields:

        - ``query_duration_ms`` — perf_counter delta around the operation
        - ``attempt`` — 0 for the first try, 1 for the post-reconnect retry
        - ``sql_hash`` — stable fingerprint of ``sql_for_hash`` so the
          three attempt records can be correlated without exposing the
          full SQL

        On a connection-class failure (per ``_is_connection_error``) the
        cached connection is discarded, a fresh one is opened, and
        ``operation`` runs once more. On any other ``pyodbc.Error`` — or
        a second connection-class failure — a ``FabricQueryError`` is
        raised carrying the Fabric-specific hint (if any) and the
        originating SQLSTATE.

        ``retry_on_connection_error`` gates the reconnect-and-retry. Reads
        (idempotent) keep the default ``True``. Writes pass ``False``:
        under ``autocommit=True`` the Fabric server may have committed the
        statement before a mid-flight drop (``08S01``) or client timeout
        (``HYT00``) is observed, so re-executing would double-apply a
        non-idempotent write. Instead the connection is discarded and a
        ``WRITE_STATE_UNKNOWN`` ``FabricQueryError`` is raised so the caller
        can reconcile rather than silently duplicate the write.

        Concurrency: the cached connection is serialised with ``_lock`` —
        pyodbc connections are not safe for concurrent cursor use, and
        FastMCP's ``streamable-http`` transport can dispatch sync tools
        to multiple worker threads.
        """
        h = _sql_hash(sql_for_hash)
        with self._lock:
            for attempt in (0, 1):
                t0 = time.perf_counter()
                try:
                    result, row_count = operation(self._get_connection())
                except pyodbc.Error as e:
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    sqlstate = e.args[0] if e.args and isinstance(e.args[0], str) else None
                    if attempt == 0 and _is_connection_error(e):
                        if not retry_on_connection_error:
                            logger.error(
                                "Connection lost during write; commit state unknown, "
                                "not retrying to avoid duplicate application",
                                extra={
                                    "operation": op_label,
                                    "sqlstate": sqlstate or "",
                                    "attempt": attempt,
                                    "query_duration_ms": duration_ms,
                                    "sql_hash": h,
                                    "error_detail": str(e),
                                },
                            )
                            self._discard_connection()
                            raise FabricQueryError(
                                message=(
                                    "Connection lost during write; the statement may or "
                                    "may not have committed. It was NOT retried to avoid "
                                    "duplicate application — verify the table state before "
                                    "retrying."
                                ),
                                code="WRITE_STATE_UNKNOWN",
                                details=None,
                                sqlstate=sqlstate,
                            ) from e
                        logger.warning(
                            "Connection-class error, reconnecting and retrying",
                            extra={
                                "operation": op_label,
                                "sqlstate": sqlstate or "",
                                "attempt": attempt,
                                "query_duration_ms": duration_ms,
                                "sql_hash": h,
                            },
                        )
                        self._discard_connection()
                        continue
                    raw = str(e)
                    logger.error(
                        "%s failed after attempt %d",
                        op_label,
                        attempt,
                        extra={
                            "operation": op_label,
                            "sqlstate": sqlstate or "",
                            "attempt": attempt,
                            "query_duration_ms": duration_ms,
                            "sql_hash": h,
                            # Full raw error retained server-side only; the
                            # client-facing message below is identifier-scrubbed.
                            "error_detail": raw,
                        },
                    )
                    message = self._redact_identifiers(raw)
                    raise FabricQueryError(
                        message=message,
                        details=_hint_for_fabric_error(message),
                        sqlstate=sqlstate,
                    ) from e
                else:
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    logger.info(
                        "%s executed: %d rows",
                        op_label.capitalize(),
                        row_count,
                        extra={
                            "operation": op_label,
                            "row_count": row_count,
                            "query_duration_ms": duration_ms,
                            "sql_hash": h,
                            "attempt": attempt,
                        },
                    )
                    return result
            raise RuntimeError("unreachable")  # pragma: no cover

    def execute_query(
        self, sql: str, timeout: int = 30, max_rows: int | None = None
    ) -> tuple[list[ColumnInfo], list[dict[str, object]]]:
        """Execute a read-only SQL query and return column metadata and rows.

        Returns (columns, rows) where rows are dicts keyed by column name.
        Raises FabricQueryError on failure (code, message, details, sqlstate).

        ``max_rows`` bounds the client-side fetch: at most ``max_rows + 1`` rows
        are materialised (the ``+ 1`` lets the caller still detect truncation)
        instead of draining the entire result set into memory — important on
        memory-limited replicas where a large ``SELECT`` could otherwise OOM the
        process. ``None`` (e.g. internal schema queries) drains as before.
        """
        fetch_limit = None if max_rows is None else max_rows + 1

        def op(
            conn: pyodbc.Connection,
        ) -> tuple[tuple[list[ColumnInfo], list[dict[str, object]]], int]:
            conn.timeout = timeout
            cursor = conn.cursor()
            cursor.arraysize = _FETCH_BATCH_SIZE
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
            rows: list[dict[str, object]] = []
            while True:
                chunk = cursor.fetchmany(_FETCH_BATCH_SIZE)
                if not chunk:
                    break
                rows.extend(dict(zip(col_names, row, strict=False)) for row in chunk)
                if fetch_limit is not None and len(rows) >= fetch_limit:
                    # Stop draining once we have cap+1; the caller truncates to
                    # cap and reports truncated=true.
                    del rows[fetch_limit:]
                    break
            return (columns, rows), len(rows)

        return self._execute_with_retry(op, op_label="query", sql_for_hash=sql)

    def execute_write(self, sql: str) -> int:
        """Execute a write SQL statement (INSERT/UPDATE) and return affected row count.

        Raises FabricQueryError on failure (code, message, details, sqlstate).
        """
        def op(conn: pyodbc.Connection) -> tuple[int, int]:
            # Deterministic write timeout: 0 = unbounded (the original
            # no-explicit-write-timeout intent). Without this, a write would
            # inherit the residual conn.timeout left by the last execute_query
            # on the shared cached connection, making write timeouts
            # nondeterministic and manufacturing spurious HYT00 aborts.
            conn.timeout = 0
            cursor = conn.cursor()
            cursor.execute(sql)
            affected = cursor.rowcount
            return affected, affected

        # Writes are not auto-retried on connection-class errors: under
        # autocommit a mid-flight drop/timeout may follow a server-side commit,
        # so a retry would double-apply a non-idempotent write.
        return self._execute_with_retry(
            op, op_label="write", sql_for_hash=sql, retry_on_connection_error=False
        )

    def execute_writes(self, sqls: list[str]) -> list[int | FabricQueryError]:
        """Execute a batch of write SQL statements under a single lock + cursor.

        Returns a list aligned to ``sqls``: each slot is either the affected
        row count (success) or a ``FabricQueryError`` (failure). Per-statement
        errors do NOT abort the batch — later statements still execute.

        Connection-class errors are NOT retried for the failing statement:
        under ``autocommit`` a mid-flight drop/timeout may follow a server-side
        commit, so re-executing would double-apply a non-idempotent write. The
        failing slot is recorded as a ``WRITE_STATE_UNKNOWN`` ``FabricQueryError``,
        the dead connection is discarded, and a fresh one is built lazily for the
        *next* statement so subsequent statements still run. Query-side errors
        keep the connection and the batch continues on it.

        Why this exists instead of looping ``execute_write`` at the call site:
        each ``execute_write`` re-acquires ``_lock``, re-runs the retry stack,
        and allocates a new cursor (an ODBC SQLAllocHandle round-trip). On a
        100-statement batch those fixed overheads dominate Fabric round-trip
        time. This helper amortises them across the batch — one lock, one
        cursor — while preserving per-statement error attribution.
        """
        if not sqls:
            return []

        results: list[int | FabricQueryError] = []
        with self._lock:
            cursor: pyodbc.Cursor | None = None
            for idx, sql in enumerate(sqls):
                if cursor is None:
                    conn = self._get_connection()
                    # Deterministic write timeout — see execute_write.
                    conn.timeout = 0
                    cursor = conn.cursor()
                sql_h = _sql_hash(sql)
                t0 = time.perf_counter()
                try:
                    cursor.execute(sql)
                    affected = cursor.rowcount
                except pyodbc.Error as e:
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    sqlstate = (
                        e.args[0] if e.args and isinstance(e.args[0], str) else None
                    )
                    raw = str(e)
                    if _is_connection_error(e):
                        logger.error(
                            "Batch write: connection lost at index %d; commit state "
                            "unknown, not retrying to avoid duplicate application",
                            idx,
                            extra={
                                "operation": "write_batch",
                                "sqlstate": sqlstate or "",
                                "query_duration_ms": duration_ms,
                                "sql_hash": sql_h,
                                "batch_index": idx,
                                "error_detail": raw,
                            },
                        )
                        # Drop the dead connection; rebuild lazily for the next
                        # statement (cursor=None). Do NOT re-execute this one.
                        self._discard_connection()
                        cursor = None
                        results.append(
                            FabricQueryError(
                                message=(
                                    "Connection lost during write; the statement may or "
                                    "may not have committed. It was NOT retried to avoid "
                                    "duplicate application — verify the table state before "
                                    "retrying."
                                ),
                                code="WRITE_STATE_UNKNOWN",
                                details=None,
                                sqlstate=sqlstate,
                            )
                        )
                        continue
                    logger.error(
                        "Batch write failed at index %d",
                        idx,
                        extra={
                            "operation": "write_batch",
                            "sqlstate": sqlstate or "",
                            "query_duration_ms": duration_ms,
                            "sql_hash": sql_h,
                            "batch_index": idx,
                            "error_detail": raw,
                        },
                    )
                    message = self._redact_identifiers(raw)
                    results.append(
                        FabricQueryError(
                            message=message,
                            details=_hint_for_fabric_error(message),
                            sqlstate=sqlstate,
                        )
                    )
                    continue
                else:
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    logger.info(
                        "Batch write executed at index %d: %d rows",
                        idx,
                        affected,
                        extra={
                            "operation": "write_batch",
                            "row_count": affected,
                            "query_duration_ms": duration_ms,
                            "sql_hash": sql_h,
                            "batch_index": idx,
                        },
                    )
                    results.append(affected)
        return results
