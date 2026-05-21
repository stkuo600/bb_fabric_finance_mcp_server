"""MCP tool for executing read-only SQL queries against Fabric data warehouse."""

from __future__ import annotations

import json
import logging
import re

from mcp.server.fastmcp import FastMCP

from src.config import FabricSettings
from src.database import FabricDatabase
from src.models import ErrorResponse, QueryResult

logger = logging.getLogger("fabric_mcp.tools.query")

_READ_PREFIX = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
_WRITE_DML = re.compile(r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


def _strip_literals_and_comments(sql: str) -> str:
    """Strip SQL comments and string/identifier literals so a subsequent
    keyword scan does not match text inside them.

    Handles: `--` line comments, `/* */` block comments, `'...'` strings
    (with `''` escape), `[...]` and `"..."` quoted identifiers.
    """
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r"\[[^\]]*\]", "[]", sql)
    sql = re.sub(r'"[^"]*"', '""', sql)
    return sql


def _is_read_only_query(sql: str) -> bool:
    """Accept SELECT or `WITH ... SELECT`. Reject if any outer-statement
    write DML keyword (INSERT/UPDATE/DELETE/MERGE) appears.

    CTE bodies are grammatically SELECT-only in T-SQL, so a write keyword
    surviving the literal/comment strip signals a write at the outer
    statement (e.g. `WITH cte AS (...) INSERT INTO ...`).
    """
    if not _READ_PREFIX.match(sql):
        return False
    cleaned = _strip_literals_and_comments(sql)
    return _WRITE_DML.search(cleaned) is None


def register_query_tools(mcp: FastMCP, db: FabricDatabase, config: FabricSettings) -> None:
    """Register query-related MCP tools."""

    @mcp.tool()
    def fabric_execute_query(sql: str) -> str:
        """Execute a read-only SQL SELECT query against the Fabric data warehouse.

        Returns query results as a JSON array of objects with column metadata.
        Only SELECT statements are accepted; other statement types are rejected.

        Args:
            sql: SQL SELECT statement to execute.
        """
        logger.info("Query requested", extra={"tool": "fabric_execute_query"})

        if not _is_read_only_query(sql):
            error = ErrorResponse(
                code="INVALID_OPERATION",
                message=(
                    "Only read-only queries are allowed: a SELECT, or a CTE-prefixed "
                    "`WITH ... SELECT`. Use fabric_preview_write for INSERT/UPDATE."
                ),
            )
            logger.warning(
                "Non-read-only rejected",
                extra={"tool": "fabric_execute_query", "error_code": "INVALID_OPERATION"},
            )
            return error.model_dump_json()

        try:
            columns, rows = db.execute_query(sql, timeout=30)
        except RuntimeError as e:
            logger.error("Query failed", extra={"tool": "fabric_execute_query", "error_code": "QUERY_ERROR"})
            return str(e)

        truncated = len(rows) > config.max_rows
        if truncated:
            rows = rows[: config.max_rows]

        result = QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )

        logger.info(
            "Query completed: %d rows (truncated=%s)",
            result.row_count,
            result.truncated,
            extra={"tool": "fabric_execute_query", "row_count": result.row_count},
        )
        return json.dumps(result.model_dump(), default=str)
