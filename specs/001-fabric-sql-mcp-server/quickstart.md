# Quickstart: Fabric SQL MCP Server

## Prerequisites

- Python 3.11+
- ODBC Driver 18 for SQL Server ([download](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server))
- Azure AD app registration with access to a Microsoft Fabric data warehouse
- Microsoft Fabric workspace with a provisioned data warehouse and SQL endpoint enabled

## Local Development Setup

1. **Clone and install dependencies**:
   ```bash
   git clone <repo-url>
   cd bb_fabric_finance_mcp_server
   pip install -e ".[dev]"
   ```

2. **Create config file** (`config.json` in project root — gitignored):
   ```json
   {
     "fabric": {
       "server": "your-fabric-server.datawarehouse.fabric.microsoft.com",
       "database": "gold_warehouse",
       "client_id": "your-client-id",
       "client_secret": "your-client-secret",
       "tenant_id": "your-tenant-id",
       "write_allowlist": ["dbo.my_table"],
       "max_rows": 500
     }
   }
   ```

3. **Run the server**:
   ```bash
   python -m src.server
   ```
   Server starts at `http://localhost:8000/mcp`

4. **Run tests**:
   ```bash
   pytest tests/unit/          # Unit tests (no Fabric connection needed)
   pytest tests/contract/      # MCP tool contract tests
   pytest tests/integration/   # Integration tests (requires Fabric connection)
   ```

## Production Deployment (Azure Container Apps)

Set environment variables:
```
FABRIC_SERVER=your-fabric-server.datawarehouse.fabric.microsoft.com
FABRIC_DATABASE=gold_warehouse
FABRIC_CLIENT_ID=your-client-id
FABRIC_CLIENT_SECRET=your-client-secret
FABRIC_TENANT_ID=your-tenant-id
FABRIC_WRITE_ALLOWLIST=dbo.table1,dbo.table2
FABRIC_MAX_ROWS=500
FABRIC_PORT=8000
```

## MCP Tools Available

| Tool | Description |
|------|-------------|
| `fabric_execute_query` | Run a SELECT query, returns JSON array of objects |
| `fabric_preview_write` | Preview an INSERT/UPDATE, get confirmation token |
| `fabric_execute_write` | Execute a confirmed write operation |
| `fabric_list_tables` | List all tables and views |
| `fabric_describe_table` | Get column details for a table |
