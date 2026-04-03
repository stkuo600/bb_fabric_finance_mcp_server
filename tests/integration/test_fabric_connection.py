"""Integration tests for full Fabric connection flow.

These tests require a real Fabric connection. Skip in CI unless credentials are configured.
Set FABRIC_SERVER, FABRIC_DATABASE, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET, FABRIC_TENANT_ID
environment variables to run these tests.
"""

from __future__ import annotations

import os

import pytest

FABRIC_CONFIGURED = all(
    os.environ.get(key)
    for key in ["FABRIC_SERVER", "FABRIC_DATABASE", "FABRIC_CLIENT_ID", "FABRIC_CLIENT_SECRET", "FABRIC_TENANT_ID"]
)

skip_no_fabric = pytest.mark.skipif(not FABRIC_CONFIGURED, reason="Fabric credentials not configured")


@skip_no_fabric
class TestFabricIntegration:
    """End-to-end integration tests against a real Fabric warehouse."""

    def test_auth_and_connect(self) -> None:
        from src.auth import FabricAuth
        from src.config import load_config

        config = load_config()
        auth = FabricAuth(config.tenant_id, config.client_id, config.client_secret)
        token = auth.get_token()
        assert token is not None
        assert len(token) > 0

    def test_query_information_schema(self) -> None:
        from src.auth import FabricAuth
        from src.config import load_config
        from src.database import FabricDatabase

        config = load_config()
        auth = FabricAuth(config.tenant_id, config.client_id, config.client_secret)
        db = FabricDatabase(config.server, config.database, auth)

        columns, rows = db.execute_query("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA")
        assert len(columns) == 1
        assert columns[0].name == "SCHEMA_NAME"
        schema_names = [row["SCHEMA_NAME"] for row in rows]
        assert "raw" in schema_names or "gold" in schema_names

    def test_list_schemas_tool(self) -> None:
        import json

        from mcp.server.fastmcp import FastMCP

        from src.auth import FabricAuth
        from src.config import load_config
        from src.database import FabricDatabase
        from src.tools.schema import register_schema_tools

        config = load_config()
        auth = FabricAuth(config.tenant_id, config.client_id, config.client_secret)
        db = FabricDatabase(config.server, config.database, auth)

        mcp = FastMCP("test")
        register_schema_tools(mcp, db)

        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "fabric_list_schemas":
                tool_fn = tool.fn
                break

        assert tool_fn is not None
        result = json.loads(tool_fn())
        assert "schemas" in result
