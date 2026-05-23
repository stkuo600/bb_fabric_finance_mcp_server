## Root Cause

The MCP server's pyodbc connection string omits TCP keep-alive
configuration, so the ODBC driver defaults to `KeepAlive=30s` (idle
threshold before sending the first keep-alive probe; Microsoft Learn:
*Connecting from Linux or macOS — Adjusting the TCP Keep-Alive
Settings*).

Heavy Fabric-side compute (e.g. the aggregating view
`gold.vw_Sch1X_EntityUSD` over one fiscal period of all entities) takes
~18-22s during which no TCP traffic flows between the MCP container and
the Fabric SQL endpoint. Some Azure intermediary in the path (most
likely SNAT or the Fabric Warehouse front-end) drops the silent TCP
connection at ~T+20s — **before** the driver's first 30s keep-alive
probe is scheduled.

Driver detects the RST/closed socket at the next read attempt and
surfaces `08S01` ("Communication link failure"). The application-level
`_execute_with_retry` (`src/database.py`) classifies `08S01` as a
connection-class error, discards the cached connection, opens a fresh
one, and retries the same SQL — which hits the same TCP-idle drop after
another ~20s. Net user-visible failure: ~40s, no rows returned, no
hint that the query itself was healthy on the server side.

This is the failure mode Microsoft Learn explicitly documents at
*Troubleshoot the Warehouse → "A transport-level error has occurred when
receiving results from the server"*: "The server terminates the
connection unexpectedly, usually due to network instability, timeout,
or server-side resource limits. … Split the copy into partitions
instead of a long-running copy."

Splitting the query is a caller-side workaround. The fix here addresses
the client-side TCP layer so the connection stays observably alive
through Fabric-side compute.

## Proposed Fix

Two layers, no behavioural change to callers:

### Layer 1 — TCP keep-alive

Add `KeepAlive=10;KeepAliveInterval=1` to the connection string. The
ODBC driver will send a keep-alive probe after 10s of TCP idleness
(half the observed ~20s drop threshold, leaving comfortable headroom).
Subsequent probes follow at 1s intervals if a response isn't received.

Per Microsoft Learn (*DSN and Connection String Keywords and
Attributes*), `KeepAlive` and `KeepAliveInterval` are supported as
connection-string keywords in ODBC Driver 17.8+ (driver 18 inherits).
Zero application overhead — TCP keep-alive probes are tiny (no payload)
and only emitted during true idleness.

### Layer 2 — Application Name for server-side telemetry

Add `APP=fabric-mcp` (or `Application Name=fabric-mcp`) so the
warehouse's `queryinsights.exec_requests_history.program_name` carries
a value an operator can filter on. Cost: none. Benefit: DBAs can
correlate failures to this MCP server without scraping container logs.

### Layer 3 — Per-attempt observability (user requested)

Instrument `_execute_with_retry` so every attempt logs:

- `query_duration_ms` (perf_counter delta around `operation(conn)`)
- `attempt` (0 or 1; the 1 means we already discarded once)
- `sql_hash` (first 16 hex chars of SHA-256 of the SQL string) on both
  the retry warning and the FabricQueryError path, plus on success

Carried fields make it possible to answer "did query X die at the same
~20s mark twice?" by reading one log line per attempt, without
cross-referencing wall-clock timestamps. `sql_hash` lets us match
attempt 0 to attempt 1 to caller-visible failure without logging the
full SQL (which can contain large IN-lists or PII).

### Out of scope (deferred as residual risks)

- **Smarter retry policy** (skip retry if attempt 0 took > N seconds, so
  the mid-query drop case fails fast instead of paying 40s). Useful if
  Layer 1 only mitigates rather than eliminates the drop, but it's a
  behaviour change with its own trade-offs (would mask stale-connection
  recovery for legitimate idle disconnects). Revisit if Layer 1
  insufficient.
- **MS Learn guidance for true Fabric transient errors** (*"don't
  immediately retry, wait 5-10 minutes"*) — applies to multi-second
  reconfiguration events, not the 20s TCP drop we observe. Out of scope
  for this fix.

## Files to Modify

- `src/database.py`
  - `FabricDatabase.__init__`: extend `_connection_string` with
    `KeepAlive=10;KeepAliveInterval=1;APP=fabric-mcp`.
  - `_execute_with_retry`: measure per-attempt duration via
    `time.perf_counter()`. Compute `sql_hash` once per call (operations
    that don't run SQL don't need it, but the helper is currently only
    used for SQL ops). Add fields to warning + error log payloads.
  - `execute_query` / `execute_write`: pass `sql` (already in closure) to
    `_execute_with_retry` via a new `sql_for_hash` parameter so the
    helper can derive the hash without re-parsing the operation.

- `tests/unit/test_database.py`
  - `TestFabricDatabase`: add `test_connection_string_enables_tcp_keepalive`
    and `test_connection_string_carries_application_name`.
  - `TestExecuteWithRetry`: assert `query_duration_ms` field appears in
    success logs and in the FabricQueryError raise path; assert
    `sql_hash` is included; assert `attempt` field present.

## Test Strategy

### Unit-test layer

Three new pure tests covering the *deterministic* part of the fix:

1. Connection string contains `KeepAlive=10` and `KeepAliveInterval=1`.
2. Connection string contains `APP=` keyword (test asserts the keyword
   present; value not asserted to allow renaming).
3. On a successful query, the success log line includes
   `query_duration_ms` (positive int).
4. On retry-then-fail, the final `FabricQueryError` log includes
   `query_duration_ms`, `attempt`, and `sql_hash` fields.

Existing tests must continue passing — particularly the contract tests
asserting client-facing wire format and the stale-connection recovery
tests asserting the retry policy still discards on 08*/IMC*/HYT*.

### Production-validation layer (post-deploy)

Run the same `gold.vw_Sch1X_EntityUSD` query for FY=2026/FM=4 across all
entities against the new revision. Three possible outcomes:

- **Success.** Hypothesis confirmed: KeepAlive prevents the TCP drop.
- **Same 08S01 at ~20s.** Hypothesis wrong: the drop isn't TCP-idle.
  Likely Fabric server-side query timeout; need to split the query
  caller-side per MS recommendation. The observability changes still
  pay off — they make the next diagnosis cheaper.
- **Different timing / different error.** Re-run Phase 1.
