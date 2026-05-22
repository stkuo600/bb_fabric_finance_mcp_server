## Symptom

After the MCP server has been idle for some time (typically tens of minutes
to hours), the first incoming tool call fails with a pyodbc error, and
**every subsequent tool call also fails** — including trivial ones like
`SELECT 1`. The only known remediation is restarting the MCP server
container.

User-observed error chain on the first failure:

```
HY000  → Unspecified error, connection terminated by server
08S01  → TCP Provider: Error code 0x2746 (connection reset)
IMC06  → Connection marked as unrecoverable by client driver
```

After the first failure, every subsequent call returns the same
`IMC06`-bearing pyodbc error — the connection never self-heals.

## Environment

- Production deployment: Azure Container Apps (single replica)
- MCP server: long-lived FastMCP `streamable-http` process
- Backend: Microsoft Fabric Warehouse (`*.datawarehouse.fabric.microsoft.com`)
- Driver: ODBC Driver 18 for SQL Server
- Connection string (pre-fix), `src/database.py:82-88`:
  ```
  DRIVER={ODBC Driver 18 for SQL Server};
  SERVER=...,1433;
  DATABASE=...;
  Encrypt=yes;
  TrustServerCertificate=no
  ```
  (No `ConnectRetryCount` / `ConnectRetryInterval` — driver-level idle
  resiliency is disabled.)
- Connection cache: single shared `pyodbc.Connection` cached on
  `FabricDatabase._conn`, serialised by `_lock`. Reconnect is **only**
  attempted when `_is_connection_error(exc)` returns True, which gates
  on `exc.args[0].startswith("08")` (`src/database.py:30-38`).
- Reactive-retry logic introduced by the prior bugfix
  `.claude/bugfix/2026-05-22-slow-execution-no-conn-reuse/` — see
  Residual Risk #2 of that report, which explicitly anticipated this
  failure mode ("reactive, not proactive… the next call pays one extra
  round trip").

## Reproduction Steps

### Manual (production-only, non-deterministic)

Stable network-level reproduction is **not feasible** in CI/local because
it requires waiting for Fabric (or an intervening load balancer) to drop
the idle TCP connection — typically tens of minutes of true idleness on
a non-deterministic schedule. The reproduction below describes the
observed production sequence; the deterministic repro lives in the
unit-test section.

1. Deploy the MCP server (any healthy instance).
2. Issue one successful query (e.g. `fabric_schemas`) to populate the
   shared `self._conn`.
3. Leave the server idle long enough that the Fabric side closes the TCP
   connection (observed in production after multi-hour idle windows).
4. Issue any tool call (e.g. `fabric_execute_query("SELECT 1")`).
5. **Observed:** the call fails with a pyodbc error whose diagnostic
   chain includes `HY000`, `08S01`, `IMC06`.
6. Issue any subsequent tool call (within seconds, no further idle
   period).
7. **Observed:** every subsequent call returns the same `IMC06`-class
   error. The error persists indefinitely until the container is
   restarted.

### Unit-test (deterministic)

Mock `pyodbc.connect(...).cursor().execute(...)` to raise
`pyodbc.Error("HY000", "...connection forcibly closed...")` (or
`pyodbc.Error("IMC06", "...")`) on the first call. Assert that:

- Under current `src/database.py`, the first call fails as
  `RuntimeError(QUERY_ERROR)` AND `self._conn` is **not** discarded
  (`pyodbc.connect.call_count == 1`).
- A second `execute_query` call therefore reuses the stale connection
  and, in a realistic mock, fails identically — confirming the
  "permanent breakage" half of the symptom.

This is a behavioural test, not an idle-timeout test. It targets the
defective classification logic, which is the actual root cause of the
permanent-breakage half of the symptom — the part that requires a
container restart.

## Expected vs Actual Behavior

| | Expected | Actual (current code) |
|---|---|---|
| First failed query after idle disconnect | Driver-level idle resiliency silently reconnects and serves the query, OR application discards `self._conn`, opens a new one, retries once, returns the result. | Driver-level resiliency is disabled. Application classifier matches `08*` only; `HY000` / `IMC06` skip the retry path. Connection is **not** discarded. Caller sees `QUERY_ERROR`. |
| Second query (seconds later) | Succeeds via cached or freshly-opened connection. | Reuses the same stale `self._conn`. Driver returns `IMC06` immediately (no round trip — client-side unrecoverable marker per Microsoft docs). Caller sees `QUERY_ERROR`. |
| Recovery | Automatic on next query, at most one extra round trip. | Manual container restart. |
