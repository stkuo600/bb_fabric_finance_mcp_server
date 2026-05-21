## Root Cause

`FabricDatabase._get_connection()` in `src/database.py` opened a brand-new
pyodbc connection on every invocation, and `execute_query`/`execute_write`
closed that connection in a `finally` block. The MCP server runs as a
long-lived process under FastMCP's `streamable-http` transport, and
`db = FabricDatabase(...)` is instantiated once at module load
(`src/server.py:27`), so the design already supported a shared connection —
but the code threw the connection away after every tool call.

Each fresh pyodbc.connect to a Fabric SQL endpoint
(`*.datawarehouse.fabric.microsoft.com`) costs a full TCP + TLS
(`Encrypt=yes`) + SQL Server pre-login + access-token-auth handshake,
typically 500 ms – 2 s from an Azure region. For sub-second queries
(e.g. `SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA`) this overhead
dominates wall-clock latency by an order of magnitude, which the user
observed as "執行很慢".

MSAL token acquisition was ruled out as the bottleneck:
`FabricAuth.get_token()` calls `acquire_token_silent` first and returns
from MSAL's in-memory cache when valid. ODBC Driver Manager pooling could
not be relied on because the encoded `SQL_COPT_SS_ACCESS_TOKEN` byte string
participates in the pool-key match and behaviour differs across platforms.

## Fix Description

`src/database.py` now caches a single long-lived pyodbc connection on the
`FabricDatabase` instance and reuses it across all tool calls:

- New instance state: `self._conn: pyodbc.Connection | None = None` and
  `self._lock = threading.Lock()`. The lock serialises access to the
  shared connection — pyodbc connections are not safe for concurrent
  cursor use, and FastMCP can dispatch sync tools to multiple worker
  threads under `streamable-http`.
- `_get_connection()` now returns the cached connection, lazily opening
  it via `_open_connection()` (the old body) when `None`.
- `_discard_connection()` best-effort closes and resets the cached
  connection (swallows `pyodbc.Error` via `contextlib.suppress`).
- `execute_query` and `execute_write` are wrapped in `with self._lock:`
  and a two-attempt loop. On the first attempt, if `pyodbc.Error` is
  raised with a SQLSTATE in the ISO SQL "connection exception" class
  (`args[0].startswith("08")` — e.g. `08S01` communication link failure),
  the cached connection is discarded and the operation is retried once
  with a fresh connection. Any other `pyodbc.Error`, or a second-attempt
  failure, is wrapped in `RuntimeError(QUERY_ERROR ...)` as before.
- `conn.close()` is no longer called in the per-call `finally` block.
  The connection lives for the process lifetime (or until a connection-
  class error forces a reconnect).

The behavioural surface for callers (`src/tools/*.py`) is unchanged:
return types, error semantics, and `QUERY_ERROR` code are identical.

## Tests Added

Five new tests in `tests/unit/test_database.py::TestConnectionReuse`:

1. `test_execute_query_reuses_connection_across_calls` — five
   `execute_query` calls produce exactly one `pyodbc.connect`.
2. `test_execute_write_reuses_connection_across_calls` — same for writes.
3. `test_mixed_query_and_write_reuse_connection` — interleaved reads and
   writes share one connection.
4. `test_reconnects_on_connection_class_sqlstate` — when the first
   `cursor.execute` raises `pyodbc.Error("08S01", ...)`, the cached
   connection is closed and a new one is opened transparently; the
   caller sees the result of the retried call.
5. `test_query_error_does_not_discard_connection` — a non-08 SQLSTATE
   (`42S02` invalid object name) surfaces as `RuntimeError(QUERY_ERROR)`
   and does **not** trigger a reconnect; a subsequent successful call
   still uses the cached connection.

Verified RED: all 5 failed against the pre-fix code with the expected
assertion (`mock_connect.call_count == 5` instead of `1`, etc.). Verified
GREEN: all 5 pass after the fix.

Full suite result after fix:

```
79 passed, 3 skipped in 5.22s
```

The 3 skips are `tests/integration/test_fabric_connection.py` which
require live Fabric credentials and are skipped in CI/local without
them — same as before the fix. `ruff check src/ tests/` is clean.

## Residual Risks

1. **Serialisation under load.** A single shared connection plus a
   `threading.Lock` means concurrent tool calls run one at a time.
   Throughput is still vastly better than the previous per-call
   reconnect, but a future high-concurrency workload may justify a small
   connection pool. Not addressed in this fix per the "minimal change"
   rule.

2. **Idle disconnect detection is reactive, not proactive.** If Fabric
   (or an intervening load balancer) drops the idle connection between
   tool calls, the next call pays one extra round trip: it fails with
   an 08-class SQLSTATE, the connection is discarded, and the operation
   is retried. The user-visible call still succeeds. A keep-alive ping
   could pre-empt this but adds complexity and was deemed out of scope.

3. **Spec constitution mentions "Timeout + retry with exponential
   backoff for Fabric connections"** (`specs/001-fabric-sql-mcp-server/
   plan.md:31`). The fix adds a single immediate retry on connection-
   class failures (an improvement over zero retries today) but is not
   exponential. A true backoff loop should be a follow-up if flaky
   network conditions are observed.

4. **Token expiry on long-lived connections.** The access token is
   bound only at connect time via `SQL_COPT_SS_ACCESS_TOKEN`; Fabric
   does not require the token to be refreshed for an already-
   authenticated session. If Fabric ever does close the session for
   token expiry, the 08-class retry will reconnect with a freshly
   acquired token from `FabricAuth.get_token()`. No additional handling
   needed for the common path.

## Git Commits

Not committed yet — pending user confirmation. Files staged for commit:

- `src/database.py` (connection-reuse implementation)
- `tests/unit/test_database.py` (5 new tests in `TestConnectionReuse`)
- `.claude/bugfix/2026-05-22-slow-execution-no-conn-reuse/{repro,plan,report}.md`
