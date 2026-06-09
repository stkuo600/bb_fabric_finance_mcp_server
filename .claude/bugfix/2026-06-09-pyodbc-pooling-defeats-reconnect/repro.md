# Repro: pyodbc connection pooling defeats the 08S01 reconnect

## Symptom

After the Fabric SQL connection goes idle and Fabric recycles the server-side
session, **every** subsequent tool call fails with:

```
SELECT 1  →  08S01 [Microsoft][ODBC Driver 18 for SQL Server] Communication link failure (0)
```

The failure is permanent for the lifetime of the process — it never self-heals.
The application's existing connection-class retry (`_execute_with_retry`
discards the connection and reconnects on `08S01`) runs on every call but does
**not** recover. Only a full container/process restart fixes it.

Production observation (revision `--0000006`, replica `...t6jjs`, 2026-06-09):

- Last successful query in Fabric `queryinsights.exec_requests_history`
  (program_name = `fabric-mcp`): **2026-06-08 08:41:07 UTC** (210–754 ms each).
- Then ~18 hours where every query failed with `08S01`.
- The failing queries are **absent** from `exec_requests_history` → they never
  reached Fabric.
- Container logs for each failure show the full retry cycle still executing:
  `Connection-class error, reconnecting and retrying` → `Acquiring new token`
  → `Token acquired successfully` → `Connected to Fabric SQL endpoint` →
  `query failed after attempt 1`. Two ~20 s stacks (~40 s total) per call.
- `az containerapp revision restart` → next `SELECT 1` succeeded
  (`Query executed: 1 rows`, ~1.5 s). Full recovery.

## Environment

- Azure Container Apps, revision `fabric-finance-mcp-server--0000006`,
  image `bluebellmcpregistry.azurecr.io/bb-fabric-finance-mcp-server:7e67cb6`.
- Linux container, ODBC Driver 18 for SQL Server, unixODBC, pyodbc.
- `src/database.py` caches a single `pyodbc.Connection` and reconnects on
  connection-class SQLSTATEs (`08*`, `IMC*`, `HYT*`). Connection string already
  carries `ConnectRetryCount=3;ConnectRetryInterval=10` and
  `KeepAlive=10;KeepAliveInterval=1` (prior bugfixes 2026-05-22 / 2026-05-23).
- `pyodbc.pooling` is **never set** anywhere in `src/` → it stays at its
  documented default of `True`.

## Reproduction Steps

### Behavioural (production-only — see note)

1. Start the server; run a query so a connection is cached.
2. Leave it idle long enough for Fabric to recycle the server-side session
   (the cached pyodbc connection is now dead).
3. Issue any query (even `SELECT 1`). It fails `08S01`; the retry reconnects
   and fails again; every subsequent query keeps failing `08S01` forever.
4. Restart the process → the very next query succeeds.

**Note — why no behavioural unit test:** the defect is in the unixODBC
driver-manager pooling layer reusing a dead handle after a *real* idle TCP
disconnect. Reproducing it deterministically requires the real ODBC driver +
a real Fabric idle disconnect; it cannot be faked in a unit test (mocking
`pyodbc.connect` returns a fresh object each call and so cannot exhibit the
pool's dead-handle reuse). Per the Bug Fix Protocol, the behavioural repro is
documented here as manual/production, and the regression is locked by a guard
test on the fix's contract (see below).

### Guard test (deterministic, unit-level)

Assert that importing `src.database` has disabled ODBC pooling:

```python
import pyodbc
import src.database  # noqa: F401
assert pyodbc.pooling is False
```

Fails against current code (`pyodbc.pooling` is the default `True`); passes
after the fix.

## Expected vs Actual Behavior

**Expected:** when the cached connection dies, `_discard_connection()` →
`close()` truly closes it and `_open_connection()` performs a genuine fresh
handshake; the existing 08S01 retry recovers within one call. No manual
restart needed (spec SC-004: "operates continuously for 24+ hours without
manual reconnection").

**Actual:** with `pyodbc.pooling = True` (default), `close()` returns the dead
connection to pyodbc's pool instead of closing it; the immediately-following
`pyodbc.connect()` with the identical connection string draws the **same dead
handle** back out of the pool. `Connected` is logged (pooled reuse returns
instantly, no real handshake) but the query is issued on the dead socket and
fails `08S01`. The retry is silently defeated; the only thing that empties the
pool is a process restart.
