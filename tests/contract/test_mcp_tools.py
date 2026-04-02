"""Contract tests verifying MCP tool schemas match contracts/mcp-tools.md."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.config import FabricSettings
from src.database import FabricDatabase


def _get_registered_tools() -> dict[str, object]:
    """Create a test MCP server and return registered tools by name."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.query import register_query_tools
    from src.tools.schema import register_schema_tools
    from src.tools.write import register_write_tools

    mcp = FastMCP("test")
    mock_db = MagicMock(spec=FabricDatabase)
    config = FabricSettings(
        server="test.datawarehouse.fabric.microsoft.com",
        database="db",
        client_id="cid",
        client_secret="cs",
        tenant_id="tid",
        write_allowlist=["gold.test"],
    )

    register_query_tools(mcp, mock_db, config)
    register_schema_tools(mcp, mock_db)
    register_write_tools(mcp, mock_db, config)

    return {tool.name: tool for tool in mcp._tool_manager._tools.values()}


def _get_params(tool: object) -> dict:
    """Extract the JSON Schema parameters dict from a tool."""
    return tool.parameters or {}


def _get_required(tool: object) -> list[str]:
    """Get list of required parameter names from tool's JSON schema."""
    return _get_params(tool).get("required", [])


def _get_properties(tool: object) -> dict:
    """Get properties dict from tool's JSON schema."""
    return _get_params(tool).get("properties", {})


class TestToolRegistration:
    """Verify all expected tools are registered."""

    def test_all_tools_registered(self) -> None:
        tools = _get_registered_tools()
        expected = {
            "fabric_execute_query",
            "fabric_list_schemas",
            "fabric_list_tables",
            "fabric_describe_table",
            "fabric_preview_write",
            "fabric_execute_write",
        }
        assert set(tools.keys()) == expected

    def test_tool_naming_convention(self) -> None:
        """Tools must follow fabric_<verb>_<noun> pattern (Constitution III)."""
        tools = _get_registered_tools()
        for name in tools:
            assert name.startswith("fabric_"), f"Tool '{name}' must start with 'fabric_'"
            parts = name.split("_")
            assert len(parts) >= 3, f"Tool '{name}' must follow fabric_<verb>_<noun> pattern"


class TestQueryToolContract:
    """Contract: fabric_execute_query."""

    def test_has_sql_parameter(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_execute_query"]
        props = _get_properties(tool)
        assert "sql" in props

    def test_sql_is_required(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_execute_query"]
        assert "sql" in _get_required(tool)

    def test_has_description(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_execute_query"]
        assert tool.description
        assert len(tool.description) > 0


class TestSchemaToolsContract:
    """Contract: fabric_list_schemas, fabric_list_tables, fabric_describe_table."""

    def test_list_schemas_no_required_params(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_list_schemas"]
        assert _get_required(tool) == []

    def test_list_tables_has_optional_schema_filter(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_list_tables"]
        props = _get_properties(tool)
        assert "schema_name" in props
        assert "schema_name" not in _get_required(tool)

    def test_describe_table_requires_table_name(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_describe_table"]
        assert "table_name" in _get_required(tool)


class TestWriteToolsContract:
    """Contract: fabric_preview_write, fabric_execute_write."""

    def test_preview_requires_sql(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_preview_write"]
        assert "sql" in _get_required(tool)

    def test_execute_requires_confirmation_token(self) -> None:
        tools = _get_registered_tools()
        tool = tools["fabric_execute_write"]
        assert "confirmation_token" in _get_required(tool)

    def test_all_tools_have_descriptions(self) -> None:
        tools = _get_registered_tools()
        for name, tool in tools.items():
            assert tool.description, f"Tool '{name}' must have a description"
