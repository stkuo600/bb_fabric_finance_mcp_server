"""Database connection management for Microsoft Fabric SQL endpoint."""

from __future__ import annotations

import logging
import struct

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


class FabricDatabase:
    """Manages pyodbc connections to a Microsoft Fabric data warehouse."""

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

    def _get_connection(self) -> pyodbc.Connection:
        """Create a new connection with a fresh access token."""
        token = self._auth.get_token()
        token_bytes = _build_token_bytes(token)
        conn = pyodbc.connect(
            self._connection_string,
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_bytes},
            autocommit=True,
        )
        logger.info("Connected to Fabric SQL endpoint", extra={"operation": "connect"})
        return conn

    def execute_query(self, sql: str, timeout: int = 30) -> tuple[list[ColumnInfo], list[dict[str, object]]]:
        """Execute a read-only SQL query and return column metadata and rows.

        Returns (columns, rows) where rows are dicts keyed by column name.
        Raises RuntimeError with ErrorResponse JSON on failure.
        """
        try:
            conn = self._get_connection()
            try:
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
            finally:
                conn.close()
        except pyodbc.Error as e:
            error = ErrorResponse(code="QUERY_ERROR", message=str(e), details=None)
            raise RuntimeError(error.model_dump_json()) from e

    def execute_write(self, sql: str) -> int:
        """Execute a write SQL statement (INSERT/UPDATE) and return affected row count.

        Raises RuntimeError with ErrorResponse JSON on failure.
        """
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(sql)
                affected = cursor.rowcount
                logger.info(
                    "Write executed: %d rows affected",
                    affected,
                    extra={"operation": "write", "row_count": affected},
                )
                return affected
            finally:
                conn.close()
        except pyodbc.Error as e:
            error = ErrorResponse(code="QUERY_ERROR", message=str(e), details=None)
            raise RuntimeError(error.model_dump_json()) from e
