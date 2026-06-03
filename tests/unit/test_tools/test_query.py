"""Unit tests for the fabric_execute_query tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.config import FabricSettings
from src.models import ColumnInfo


def _make_config(**overrides: object) -> FabricSettings:
    defaults = {
        "server": "test.datawarehouse.fabric.microsoft.com",
        "database": "db",
        "client_id": "cid",
        "client_secret": "cs",
        "tenant_id": "tid",
        "max_rows": 500,
        "api_key": "test-api-key",
    }
    defaults.update(overrides)
    return FabricSettings(**defaults)


def _make_query_tool(
    db: MagicMock | None = None, config: FabricSettings | None = None
) -> callable:
    """Create a fabric_execute_query function with mocked dependencies."""
    from mcp.server.fastmcp import FastMCP

    mock_mcp = FastMCP("test")
    mock_db = db or MagicMock()
    cfg = config or _make_config()

    from src.tools.query import register_query_tools

    register_query_tools(mock_mcp, mock_db, cfg)

    # Extract the registered tool function
    for tool in mock_mcp._tool_manager._tools.values():
        if tool.name == "fabric_execute_query":
            return tool.fn, mock_db
    msg = "fabric_execute_query not registered"
    raise RuntimeError(msg)


class TestFabricExecuteQuery:
    """Test the query tool."""

    def test_select_returns_json_result(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": 1}, {"id": 2}],
        )

        result = json.loads(fn("SELECT id FROM test"))

        assert result["row_count"] == 2
        assert result["truncated"] is False
        assert len(result["rows"]) == 2

    def test_empty_result_set(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [],
        )

        result = json.loads(fn("SELECT id FROM empty"))

        assert result["row_count"] == 0
        assert result["rows"] == []
        assert len(result["columns"]) == 1

    def test_non_select_rejected(self) -> None:
        fn, _ = _make_query_tool()

        result = json.loads(fn("INSERT INTO test VALUES (1)"))
        assert result["code"] == "INVALID_OPERATION"

    def test_update_rejected(self) -> None:
        fn, _ = _make_query_tool()

        result = json.loads(fn("UPDATE test SET col=1"))
        assert result["code"] == "INVALID_OPERATION"

    def test_delete_rejected(self) -> None:
        fn, _ = _make_query_tool()

        result = json.loads(fn("DELETE FROM test"))
        assert result["code"] == "INVALID_OPERATION"

    def test_passes_effective_cap_to_db_execute_query(self) -> None:
        """The row cap must be pushed down to db.execute_query so the fetch is
        bounded (cap+1), not applied only after draining the whole result (P8)."""
        config = _make_config(max_rows=500)
        fn, mock_db = _make_query_tool(config=config)
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": 1}],
        )

        json.loads(fn("SELECT id FROM t"))
        assert mock_db.execute_query.call_args.kwargs.get("max_rows") == 500

        json.loads(fn("SELECT id FROM t", max_rows=10))
        assert mock_db.execute_query.call_args.kwargs.get("max_rows") == 10

    def test_truncation_when_exceeding_max_rows(self) -> None:
        config = _make_config(max_rows=2)
        fn, mock_db = _make_query_tool(config=config)
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": 1}, {"id": 2}, {"id": 3}],
        )

        result = json.loads(fn("SELECT id FROM big_table"))

        assert result["truncated"] is True
        assert result["row_count"] == 2
        assert len(result["rows"]) == 2

    def test_per_call_max_rows_override(self) -> None:
        """Caller can pass max_rows to override the server-config default."""
        config = _make_config(max_rows=500)
        fn, mock_db = _make_query_tool(config=config)
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": i} for i in range(1, 6)],
        )

        result = json.loads(fn("SELECT id FROM t", max_rows=3))
        assert result["truncated"] is True
        assert result["row_count"] == 3
        assert len(result["rows"]) == 3

    def test_per_call_max_rows_allows_smaller_than_default(self) -> None:
        config = _make_config(max_rows=500)
        fn, mock_db = _make_query_tool(config=config)
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": i} for i in range(1, 11)],
        )
        result = json.loads(fn("SELECT id FROM t", max_rows=1))
        assert result["row_count"] == 1
        assert result["truncated"] is True

    def test_per_call_max_rows_zero_or_negative_rejected(self) -> None:
        fn, _ = _make_query_tool()
        result = json.loads(fn("SELECT 1", max_rows=0))
        assert result["code"] == "INVALID_OPERATION"

        result2 = json.loads(fn("SELECT 1", max_rows=-5))
        assert result2["code"] == "INVALID_OPERATION"

    def test_per_call_max_rows_above_hard_cap_rejected(self) -> None:
        fn, _ = _make_query_tool()
        result = json.loads(fn("SELECT 1", max_rows=10001))
        assert result["code"] == "INVALID_OPERATION"

    def test_query_error_returns_error_response(self) -> None:
        from src.database import FabricQueryError

        fn, mock_db = _make_query_tool()
        mock_db.execute_query.side_effect = FabricQueryError(
            message="Syntax error", code="QUERY_ERROR", details=None, sqlstate="42000"
        )

        result = json.loads(fn("SELECT bad syntax"))
        assert result["code"] == "QUERY_ERROR"
        assert result["message"] == "Syntax error"
        assert result["details"] is None


class TestCommonTableExpression:
    """CTE-prefixed queries (`WITH ...`) must be allowed when the final
    operation is SELECT, and rejected when the WITH precedes a write DML."""

    def test_simple_with_cte_then_select_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="x", type="int", nullable=False)],
            [{"x": 1}],
        )

        result = json.loads(fn("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"))

        assert result.get("code") != "INVALID_OPERATION", (
            f"WITH-prefixed SELECT must be allowed; got {result}"
        )
        assert result["row_count"] == 1
        mock_db.execute_query.assert_called_once()

    def test_lowercase_with_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="x", type="int", nullable=False)],
            [],
        )
        result = json.loads(fn("with cte as (select 1 as x) select * from cte"))
        assert result.get("code") != "INVALID_OPERATION"

    def test_multiple_ctes_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="x", type="int", nullable=False)],
            [],
        )
        sql = (
            "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS x) "
            "SELECT * FROM a UNION ALL SELECT * FROM b"
        )
        result = json.loads(fn(sql))
        assert result.get("code") != "INVALID_OPERATION"

    def test_recursive_cte_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="n", type="int", nullable=False)],
            [],
        )
        sql = (
            "WITH cte AS ("
            "  SELECT 1 AS n UNION ALL SELECT n + 1 FROM cte WHERE n < 5"
            ") SELECT * FROM cte"
        )
        result = json.loads(fn(sql))
        assert result.get("code") != "INVALID_OPERATION"

    def test_with_then_insert_rejected(self) -> None:
        fn, _ = _make_query_tool()
        sql = "WITH cte AS (SELECT 1 AS x) INSERT INTO target SELECT x FROM cte"
        result = json.loads(fn(sql))
        assert result["code"] == "INVALID_OPERATION"

    def test_with_then_update_rejected(self) -> None:
        fn, _ = _make_query_tool()
        sql = (
            "WITH cte AS (SELECT id FROM src) "
            "UPDATE target SET col = 1 FROM cte WHERE target.id = cte.id"
        )
        result = json.loads(fn(sql))
        assert result["code"] == "INVALID_OPERATION"

    def test_with_then_delete_rejected(self) -> None:
        fn, _ = _make_query_tool()
        sql = "WITH cte AS (SELECT id FROM src) DELETE FROM target WHERE id IN (SELECT id FROM cte)"
        result = json.loads(fn(sql))
        assert result["code"] == "INVALID_OPERATION"

    def test_with_then_merge_rejected(self) -> None:
        fn, _ = _make_query_tool()
        sql = (
            "WITH cte AS (SELECT id, val FROM src) "
            "MERGE INTO target USING cte ON target.id = cte.id "
            "WHEN MATCHED THEN UPDATE SET val = cte.val"
        )
        result = json.loads(fn(sql))
        assert result["code"] == "INVALID_OPERATION"

    def test_insert_keyword_in_string_literal_does_not_falsely_block(self) -> None:
        """A SELECT whose projection happens to contain the literal string
        'INSERT' (e.g. a label column) must still be allowed."""
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="op", type="str", nullable=False)],
            [{"op": "INSERT"}],
        )
        result = json.loads(fn("SELECT 'INSERT' AS op FROM dual"))
        assert result.get("code") != "INVALID_OPERATION", (
            f"write keyword in a string literal must not trigger the safety check; got {result}"
        )

    def test_insert_keyword_in_line_comment_does_not_falsely_block(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [],
        )
        result = json.loads(fn("SELECT id FROM t -- INSERT historical note\n"))
        assert result.get("code") != "INVALID_OPERATION"

    def test_insert_keyword_in_block_comment_does_not_falsely_block(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [],
        )
        result = json.loads(fn("SELECT id /* historic: INSERT path removed */ FROM t"))
        assert result.get("code") != "INVALID_OPERATION"


class TestStackedStatementsRejected:
    """A SELECT/WITH followed by one or more `;`-separated statements must be
    rejected. SQL Server/pyodbc executes every `;`-separated statement in one
    batch, so a trailing DDL/DCL/EXEC stacked behind a leading SELECT would run
    under the privileged service principal (P3, Critical)."""

    def test_stacked_drop_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; DROP TABLE raw.Fact_Sch1X"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_truncate_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; TRUNCATE TABLE raw.Fact_ExchangeRate"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_alter_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT * FROM x; ALTER TABLE x ADD c INT"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_create_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; CREATE TABLE foo (a int)"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_grant_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; GRANT CONTROL ON DATABASE::wh TO attacker"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_exec_rejected(self) -> None:
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; EXEC sp_who"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_insert_rejected(self) -> None:
        """Already partly covered by _WRITE_DML, but must hold via the
        statement-separator rule too."""
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 1; INSERT INTO t VALUES (1)"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()

    def test_stacked_statement_with_internal_semicolon_in_literal_rejected(self) -> None:
        """The leading SELECT is legitimate, but a real second statement follows
        — the `;` inside the literal must not mask the real separator."""
        fn, mock_db = _make_query_tool()
        result = json.loads(fn("SELECT 'a;b' AS c; DROP TABLE t"))
        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_query.assert_not_called()


class TestSingleStatementSemicolonAllowed:
    """A single statement with a trailing semicolon (or semicolons confined to
    string literals / comments) is legitimate and must remain allowed."""

    def test_trailing_semicolon_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [{"id": 1}],
        )
        result = json.loads(fn("SELECT id FROM raw.Dim_Entity;"))
        assert result.get("code") != "INVALID_OPERATION", (
            f"a single trailing semicolon must be allowed; got {result}"
        )
        mock_db.execute_query.assert_called_once()

    def test_trailing_semicolon_with_whitespace_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="id", type="int", nullable=False)],
            [],
        )
        result = json.loads(fn("SELECT id FROM t ;  \n"))
        assert result.get("code") != "INVALID_OPERATION"

    def test_semicolon_inside_string_literal_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="c", type="str", nullable=False)],
            [{"c": "a;b"}],
        )
        result = json.loads(fn("SELECT 'a;b' AS c"))
        assert result.get("code") != "INVALID_OPERATION", (
            f"a semicolon inside a string literal must not be treated as a "
            f"statement separator; got {result}"
        )

    def test_cte_with_trailing_semicolon_allowed(self) -> None:
        fn, mock_db = _make_query_tool()
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="x", type="int", nullable=False)],
            [{"x": 1}],
        )
        result = json.loads(fn("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte;"))
        assert result.get("code") != "INVALID_OPERATION"
