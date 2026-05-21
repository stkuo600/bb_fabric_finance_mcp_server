"""Unit tests for write tools (preview and execute)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src.config import FabricSettings
from src.tools.write import (
    _TOKEN_VERSION,
    _is_table_allowed,
    _make_token,
    _parse_write_sql,
)


def _make_config(**overrides: object) -> FabricSettings:
    defaults = {
        "server": "test.datawarehouse.fabric.microsoft.com",
        "database": "db",
        "client_id": "cid",
        "client_secret": "cs",
        "tenant_id": "tid",
        "write_allowlist": ["gold.transactions", "gold.accounts"],
        "api_key": "test-api-key",
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

    def test_expired_token_rejected(self) -> None:
        config = _make_config()
        tools = _make_write_tools(config=config)
        execute_fn, _ = tools["fabric_execute_write"]

        # Forge a properly-signed token with an `exp` in the past.
        expired_at = datetime.now(tz=UTC) - timedelta(minutes=1)
        payload = {
            "v": _TOKEN_VERSION,
            "sql": "INSERT INTO gold.transactions (id) VALUES (1)",
            "op": "INSERT",
            "table": "gold.transactions",
            "exp": expired_at.timestamp(),
            "nonce": "0" * 32,
        }
        token = _make_token(payload, config.client_secret)

        result = json.loads(execute_fn(token))
        assert result["code"] == "TOKEN_EXPIRED"


class TestStatelessToken:
    """Confirmation tokens must be redeemable across independent server instances
    (different Container Apps replicas) that share the same configuration.

    Each `_make_write_tools()` call simulates a fresh Python process — the
    stateless token design must not rely on any in-process state.
    """

    def test_token_redeemable_on_independent_instance(self) -> None:
        """Replica A issues a token; replica B (separate registration, same
        config) must be able to redeem it."""
        config = _make_config()

        tools_a = _make_write_tools(config=config)
        preview_a, _ = tools_a["fabric_preview_write"]

        tools_b = _make_write_tools(config=config)
        execute_b, mock_db_b = tools_b["fabric_execute_write"]
        mock_db_b.execute_write.return_value = 1

        preview = json.loads(preview_a("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_b(token))

        assert result.get("code") != "TOKEN_INVALID", (
            f"token must be redeemable on a different instance; got {result}"
        )
        assert result["affected_rows"] == 1
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"
        mock_db_b.execute_write.assert_called_once_with(
            "INSERT INTO gold.transactions (id) VALUES (1)"
        )

    def test_tampered_token_rejected(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, _ = tools["fabric_execute_write"]

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]
        # Flip one character in the middle of the token
        midpoint = len(token) // 2
        flipped = "A" if token[midpoint] != "A" else "B"
        tampered = token[:midpoint] + flipped + token[midpoint + 1 :]

        result = json.loads(execute_fn(tampered))
        assert result["code"] == "TOKEN_INVALID"

    def test_token_signed_with_different_secret_rejected(self) -> None:
        """A token issued under one client_secret must not be redeemable under
        a different client_secret (simulates secret rotation between replicas
        that have not yet picked up the new value)."""
        config_a = _make_config(client_secret="secret-A")
        config_b = _make_config(client_secret="secret-B")

        tools_a = _make_write_tools(config=config_a)
        preview_a, _ = tools_a["fabric_preview_write"]

        tools_b = _make_write_tools(config=config_b)
        execute_b, _ = tools_b["fabric_execute_write"]

        preview = json.loads(preview_a("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_b(token))
        assert result["code"] == "TOKEN_INVALID"

    def test_token_replay_within_window_succeeds(self) -> None:
        """Stateless tokens are replayable within the validity window. This is
        a deliberate behavioural deviation from the original spec
        (`tasks.md:130` "one-time use invalidation") in exchange for working
        correctness across replicas. See report.md for the trade-off
        analysis."""
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, mock_db = tools["fabric_execute_write"]
        mock_db.execute_write.return_value = 1

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result1 = json.loads(execute_fn(token))
        result2 = json.loads(execute_fn(token))

        assert "affected_rows" in result1
        assert "affected_rows" in result2
        assert mock_db.execute_write.call_count == 2
