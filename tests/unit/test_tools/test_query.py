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

    def test_query_error_returns_error_response(self) -> None:
        fn, mock_db = _make_query_tool()
        error_json = json.dumps({"code": "QUERY_ERROR", "message": "Syntax error", "details": None})
        mock_db.execute_query.side_effect = RuntimeError(error_json)

        result = json.loads(fn("SELECT bad syntax"))
        assert result["code"] == "QUERY_ERROR"
