"""Unit tests for write tools (preview and execute)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src.config import FabricSettings
from src.tools.write import _is_table_allowed, _parse_write_sql, _pending_writes


def _make_config(**overrides: object) -> FabricSettings:
    defaults = {
        "server": "test.datawarehouse.fabric.microsoft.com",
        "database": "db",
        "client_id": "cid",
        "client_secret": "cs",
        "tenant_id": "tid",
        "write_allowlist": ["gold.transactions", "gold.accounts"],
    }
    defaults.update(overrides)
    return FabricSettings(**defaults)


def _make_write_tools(
    db: MagicMock | None = None, config: FabricSettings | None = None
) -> dict[str, tuple[callable, MagicMock]]:
    """Create write tool functions with mocked dependencies."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.write import register_write_tools

    mock_mcp = FastMCP("test")
    mock_db = db or MagicMock()
    cfg = config or _make_config()
    register_write_tools(mock_mcp, mock_db, cfg)

    tools = {}
    for tool in mock_mcp._tool_manager._tools.values():
        tools[tool.name] = (tool.fn, mock_db)
    return tools


class TestParseWriteSql:
    """Test SQL parsing for write operations."""

    def test_insert_detected(self) -> None:
        result = _parse_write_sql("INSERT INTO gold.transactions (id) VALUES (1)")
        assert result == ("INSERT", "gold.transactions")

    def test_update_detected(self) -> None:
        result = _parse_write_sql("UPDATE gold.accounts SET balance = 100 WHERE id = 1")
        assert result == ("UPDATE", "gold.accounts")

    def test_select_not_detected(self) -> None:
        assert _parse_write_sql("SELECT * FROM test") is None

    def test_delete_not_detected(self) -> None:
        assert _parse_write_sql("DELETE FROM test") is None

    def test_case_insensitive(self) -> None:
        result = _parse_write_sql("insert into gold.test (id) values (1)")
        assert result == ("INSERT", "gold.test")


class TestIsTableAllowed:
    """Test allowlist checking."""

    def test_allowed_table(self) -> None:
        assert _is_table_allowed("gold.transactions", ["gold.transactions"]) is True

    def test_disallowed_table(self) -> None:
        assert _is_table_allowed("raw.imports", ["gold.transactions"]) is False

    def test_empty_allowlist(self) -> None:
        assert _is_table_allowed("any_table", []) is False

    def test_case_insensitive(self) -> None:
        assert _is_table_allowed("Gold.Transactions", ["gold.transactions"]) is True


class TestFabricPreviewWrite:
    """Test the preview write tool."""

    def setup_method(self) -> None:
        _pending_writes.clear()

    def test_valid_insert_returns_preview(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO gold.transactions (id) VALUES (1)"))

        assert "confirmation_token" in result
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"
        assert "expires_at" in result

    def test_valid_update_returns_preview(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("UPDATE gold.accounts SET balance = 100"))

        assert result["operation"] == "UPDATE"
        assert result["table"] == "gold.accounts"

    def test_table_not_on_allowlist(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO raw.secret_data (id) VALUES (1)"))

        assert result["code"] == "TABLE_NOT_ALLOWED"

    def test_non_write_rejected(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("SELECT * FROM gold.transactions"))

        assert result["code"] == "INVALID_OPERATION"

    def test_empty_allowlist_rejects_all(self) -> None:
        config = _make_config(write_allowlist=[])
        tools = _make_write_tools(config=config)
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO gold.transactions (id) VALUES (1)"))

        assert result["code"] == "TABLE_NOT_ALLOWED"


class TestFabricExecuteWrite:
    """Test the execute write tool."""

    def setup_method(self) -> None:
        _pending_writes.clear()

    def test_valid_token_executes_write(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, mock_db = tools["fabric_execute_write"]
        mock_db.execute_write.return_value = 1

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_fn(token))

        assert result["affected_rows"] == 1
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"

    def test_invalid_token_rejected(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_execute_write"]

        result = json.loads(fn("nonexistent-token"))

        assert result["code"] == "TOKEN_INVALID"

    def test_token_single_use(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, mock_db = tools["fabric_execute_write"]
        mock_db.execute_write.return_value = 1

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        # First use succeeds
        result1 = json.loads(execute_fn(token))
        assert "affected_rows" in result1

        # Second use fails
        result2 = json.loads(execute_fn(token))
        assert result2["code"] == "TOKEN_INVALID"

    def test_expired_token_rejected(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, _ = tools["fabric_execute_write"]

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        # Manually expire the token
        _pending_writes[token]["expires_at"] = datetime.now(tz=UTC) - timedelta(minutes=1)

        result = json.loads(execute_fn(token))
        assert result["code"] == "TOKEN_EXPIRED"
