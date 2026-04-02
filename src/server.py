"""FastMCP server setup for the Fabric SQL MCP Server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.auth import FabricAuth
from src.config import load_config
from src.database import FabricDatabase
from src.logging_setup import setup_logging

setup_logging()

config = load_config()

auth = FabricAuth(
    tenant_id=config.tenant_id,
    client_id=config.client_id,
    client_secret=config.client_secret,
)

db = FabricDatabase(
    server=config.server,
    database=config.database,
    auth=auth,
)

mcp_server = FastMCP(
    "Fabric SQL MCP Server",
    stateless_http=True,
    json_response=True,
)

# Import tools to register them with the server
from src.tools.query import register_query_tools  # noqa: E402
from src.tools.schema import register_schema_tools  # noqa: E402
from src.tools.write import register_write_tools  # noqa: E402

register_query_tools(mcp_server, db, config)
register_schema_tools(mcp_server, db)
register_write_tools(mcp_server, db, config)


if __name__ == "__main__":
    mcp_server.run(transport="streamable-http", host="0.0.0.0", port=config.port)
