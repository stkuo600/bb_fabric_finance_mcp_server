## Symptom

User reports: 執行速度太慢 ("execution is too slow").

Every MCP tool call (`fabric_execute_query`, `fabric_list_schemas`, `fabric_list_tables`,
`fabric_describe_table`, `fabric_preview_write`, `fabric_execute_write`) takes noticeably
longer than the underlying SQL would suggest. Trivially small queries (e.g.
`SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA`) take seconds.

## Environment

- Server: Microsoft Fabric SQL Warehouse endpoint (`*.datawarehouse.fabric.microsoft.com`)
- Hosting: Azure Container Apps (per memory `project_deployment_target.md`)
- Client: Python 3.11+ MCP server using `pyodbc` + ODBC Driver 18 for SQL Server,
  authenticating with MSAL `acquire_token_for_client` and passing the access token
  to ODBC via `SQL_COPT_SS_ACCESS_TOKEN` (attribute id 1256).
- Transport: FastMCP `streamable-http`, `stateless_http=True`.
- Branch: master, after merge of spec 001-fabric-sql-mcp-server.

## Reproduction Steps

The codebase makes the symptom deterministic. In `src/database.py`:

1. `FabricDatabase._get_connection()` (lines 43–53) calls `pyodbc.connect(...)`
   unconditionally on every invocation.
2. `FabricDatabase.execute_query()` (lines 55–89) calls `_get_connection()`
   then `conn.close()` in a `finally` block.
3. `FabricDatabase.execute_write()` (lines 91–112) does the same.
4. Every MCP tool (`src/tools/query.py`, `src/tools/schema.py`,
   `src/tools/write.py`) eventually invokes one of those two methods, so every
   tool call pays a full connection-establishment cost.

A pyodbc connection to Fabric SQL involves: TCP handshake (1 RTT) + TLS
handshake with `Encrypt=yes` (≥2 RTTs) + SQL Server pre-login + login with
access token validation. From an Azure region this is typically 500 ms – 2 s.
For sub-second queries this overhead dominates wall-clock latency by an order
of magnitude.

Concrete reproduction (deterministic, no live Fabric required):

```python
from unittest.mock import MagicMock, patch
from src.database import FabricDatabase

mock_auth = MagicMock()
mock_auth.get_token.return_value = "tok"
db = FabricDatabase("x.datawarehouse.fabric.microsoft.com", "wh", mock_auth)

with patch("src.database.pyodbc.connect") as mock_connect:
    cursor = MagicMock()
    cursor.description = [("c", int, None, None, None, None, False)]
    cursor.fetchall.return_value = []
    mock_connect.return_value.cursor.return_value = cursor
    for _ in range(5):
        db.execute_query("SELECT 1")
    print(mock_connect.call_count)   # → 5  (every call opens a fresh connection)
```

`mock_connect.call_count == 5` after 5 calls proves the anti-pattern.

## Expected vs Actual Behavior

**Expected:** A long-lived MCP server holds an open pooled/persistent
connection to the Fabric SQL endpoint and amortises the
TCP/TLS/auth-handshake cost across many tool calls. Subsequent calls within
the same process should pay only round-trip + query-execution time.

**Actual:** Every tool call opens a brand-new pyodbc connection
(`pyodbc.connect(...)`) and closes it immediately after the query, so every
call pays the full handshake cost. ODBC Driver Manager pooling is unreliable
with `SQL_COPT_SS_ACCESS_TOKEN` (the token participates in the pool key and
behaviour differs by platform), so application-level connection reuse is
required.
