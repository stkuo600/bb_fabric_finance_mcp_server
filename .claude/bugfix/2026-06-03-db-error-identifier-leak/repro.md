# Repro — Raw ODBC error text leaks Fabric server/database identifiers to the client

## Symptom

On a non-connection pyodbc error (e.g. SQLSTATE `28000` cannot-open-database, `42000`
permission-denied) — and on a second-attempt connection failure — `_execute_with_retry` sets
`message = str(e)` and raises `FabricQueryError(message=message, ...)`. `error_envelope` serializes
`message` verbatim into the on-wire `ErrorResponse`. ODBC/Fabric diagnostics frequently embed the
fully-qualified Fabric server hostname (`*.datawarehouse.fabric.microsoft.com`) and the database name,
so those identifiers cross the trust boundary to the (authenticated) MCP client — reconnaissance-grade
information disclosure about the managed backend. `execute_writes` has the same `message = str(e)` path.

Identified by the deployment-aware codebase review as **P9 (Medium, 3/3 votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/scrub-db-error-identifiers` (off `master`)
- File: `src/database.py` — `_execute_with_retry` raise (lines ~277-282), `execute_writes` raise
- The access token is passed via a connection attribute, so `client_secret` does NOT leak; the leak is
  the server hostname + database name.
- Deterministic at the `FabricDatabase` level with a mocked pyodbc error.

## Reproduction Steps

```python
from unittest.mock import MagicMock, patch
import pyodbc
from src.database import FabricDatabase, FabricQueryError

def make():
    a = MagicMock(); a.get_token.return_value = "t"
    return FabricDatabase(server="acme-prod.datawarehouse.fabric.microsoft.com",
                          database="gold_warehouse", auth=a)

with patch("src.database.pyodbc.connect") as mc:
    msg = ("[Microsoft][ODBC Driver 18 for SQL Server]Cannot open database "
           "'gold_warehouse' requested by the login on server "
           "'acme-prod.datawarehouse.fabric.microsoft.com'.")
    mc.return_value.cursor.return_value.execute.side_effect = pyodbc.Error("28000", msg)
    try:
        make().execute_query("SELECT 1")
    except FabricQueryError as e:
        print(e.message)
```

### Observed output (2026-06-03)

```
leaks host? True | leaks db? True
```

The raised `FabricQueryError.message` contains both the Fabric hostname and the database name, which
`error_envelope` returns to the client.

## Expected vs Actual Behavior

| Aspect | Expected | Actual |
|---|---|---|
| On-wire `message` | server hostname + database name redacted | both present |
| Useful query-side text (e.g. "Invalid object name 'foo'", syntax errors) | preserved (LLM self-correction) | preserved |
| Server-side log | full raw error retained for diagnosis | sqlstate only (raw text only on the wire) |

**Why:** the raised message is `str(e)` verbatim, and ODBC diagnostics embed `self._server` /
`self._database`.

## Test feasibility

Stable failing unit test at the `FabricDatabase` level: raise a pyodbc error whose text contains the
server FQDN + database name; assert the raised `message` no longer contains them, that benign query-side
text is preserved, and that the full text is logged server-side.
