## Root Cause

`FabricDatabase._get_connection()` (`src/database.py:43-53`) unconditionally calls
`pyodbc.connect(...)` on every invocation, and `execute_query`/`execute_write`
(`src/database.py:55-89`, `91-112`) each close the connection in a `finally`
block. Every MCP tool call therefore pays a full pyodbc-connect handshake to
the Fabric SQL endpoint — TCP + TLS (`Encrypt=yes`) + SQL Server pre-login +
access-token authentication — which from an Azure region typically costs
500 ms – 2 s and dominates wall-clock latency for sub-second queries.

MSAL token acquisition is not the bottleneck (`acquire_token_silent` returns
from in-memory cache when valid). ODBC Driver Manager pooling cannot be relied
on because the encoded `SQL_COPT_SS_ACCESS_TOKEN` byte string participates in
the pool-key match and pool behaviour differs across platforms; application-
level reuse is required.

## Proposed Fix

Cache a single long-lived pyodbc connection on the `FabricDatabase` instance
and reuse it across tool calls.

1. Add `self._conn: pyodbc.Connection | None = None` and
   `self._lock = threading.Lock()` to `FabricDatabase.__init__`. The lock
   serialises access to the shared connection (pyodbc connections are not
   safe for concurrent cursor use, and FastMCP's `streamable-http` transport
   can dispatch sync tools to multiple worker threads).
2. Split `_get_connection()` into:
   - `_open_connection()` — performs the actual `pyodbc.connect(...)` with a
     fresh access token (today's body).
   - `_get_connection()` — returns the cached connection, lazily opening it
     if `None`. Called only while `self._lock` is held.
   - `_close_connection()` — best-effort closes the cached connection and
     resets `self._conn = None`. Swallows `pyodbc.Error` (idempotent).
3. Refactor `execute_query` and `execute_write` to:
   - Acquire `self._lock`.
   - Try the operation against the cached connection.
   - On `pyodbc.Error` whose SQLSTATE class is `08` ("connection exception"
     per ISO SQL) — i.e. `args[0].startswith("08")` — discard the cached
     connection and retry the operation **once** with a freshly opened
     connection. This handles the case where the server (or an intermediate
     load balancer) has closed an idle connection.
   - Do **not** close the connection in `finally`. The connection lives for
     the process lifetime.
4. Leave `conn.timeout = timeout` in place inside `execute_query` — with the
   lock held, only one query runs at a time, so the per-call setting cannot
   leak between concurrent calls.

The fix is contained to `src/database.py`. No callers change.

## Files to Modify

- `src/database.py`

## Test Strategy

New failing tests in `tests/unit/test_database.py`:

1. `test_execute_query_reuses_connection_across_calls` — call
   `db.execute_query("SELECT 1")` five times; assert
   `pyodbc.connect.call_count == 1`. This is the test that fails today
   (current code makes it 5) and proves the bug exists.
2. `test_execute_write_reuses_connection_across_calls` — same pattern for
   `execute_write`.
3. `test_mixed_query_and_write_reuse_connection` — interleave read and write;
   assert single connect.
4. `test_reconnects_on_connection_class_sqlstate` — first `cursor.execute`
   raises `pyodbc.Error("08S01", "...")`; second succeeds. Assert
   `pyodbc.connect.call_count == 2` and the call returns the second result.
5. `test_query_error_does_not_discard_connection` — `cursor.execute` raises
   `pyodbc.Error("42S02", "Invalid object name")` (a query-side error, not a
   connection error). Assert the call raises `RuntimeError("QUERY_ERROR")`
   and a subsequent successful call does **not** reconnect (i.e. the cached
   connection is preserved).

All existing tests in `test_database.py` must continue to pass without
modification (each existing test does at most one query against a fresh
`FabricDatabase`, so caching does not change observable behaviour).
