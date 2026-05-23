## Root Cause

The MCP server's pyodbc connection string omitted TCP keep-alive
configuration. The ODBC Driver 18 defaults to `KeepAlive=30s` (idle
threshold before the first keep-alive probe; Microsoft Learn:
*Connecting from Linux or macOS — Adjusting the TCP Keep-Alive Settings*).

Heavy Fabric-side compute on `gold.vw_Sch1X_EntityUSD` (the failing
query) takes ~18-22s during which no TCP traffic flows between the
container and the Fabric SQL endpoint. An Azure-side intermediary
(SNAT, Fabric front-end, or similar) drops the silent TCP connection
at ~T+20s — well before the 30s keep-alive probe fires. Driver detects
the closed socket at the next read, surfaces `08S01` ("Communication
link failure"). The application-level `_execute_with_retry` classifies
`08S01` as connection-class, discards, opens a fresh connection, and
retries the same SQL — which hits the same drop after another ~20s.
Net failure: ~40s, no rows.

Production-log timing on revision `--0000002` (5 consecutive failures
of the same query):

| Query | Start → retry warn | Retry warn → final error |
|---|---|---|
| 2 | 20.65s | 20.37s |
| 3 | 20.53s | 20.55s |
| 4 | 20.44s | 20.71s |
| 5 | 20.48s | 20.56s |

Millisecond-level consistency. The two-stack pattern (~20s + ~20s) is
deterministic, not random network. Matches the symptom Microsoft Learn
explicitly documents at *Troubleshoot the Warehouse → "A transport-level
error has occurred when receiving results from the server"*.

## Fix Description

`src/database.py`, three additions, no behavioural change to callers:

1. **TCP keep-alive in the connection string**: append
   `KeepAlive=10;KeepAliveInterval=1`. Driver sends a keep-alive probe
   after 10s of socket idleness — half the observed ~20s drop threshold,
   leaving headroom for clock skew / variable intermediary timing.
   Probes are zero-payload TCP packets; no measurable overhead.

2. **Application Name in the connection string**: append `APP=fabric-mcp`.
   Surfaces in Fabric `queryinsights.exec_requests_history.program_name`
   so DBAs can filter this MCP server's requests in server-side
   telemetry without scraping container logs.

3. **Per-attempt observability in `_execute_with_retry`**: introduces
   `_sql_hash(sql) -> str` (SHA-256 hex[:16]) and instruments every
   attempt — success, retry warning, final error — with structured
   fields `query_duration_ms`, `attempt` (0 or 1), and `sql_hash`.
   The operation contract changes to `Callable[[Connection], (T, int)]`
   so the helper can own the success log line with row count + duration
   in one place. Final failure now emits an explicit `logger.error`
   from the helper (in addition to the tool-level log in
   `error_envelope`) carrying the full diagnostic field set.

External contract unchanged: `FabricQueryError` payload (code, message,
details, sqlstate), `execute_query` / `execute_write` signatures, and
the client-facing JSON envelope are all identical. Contract tests pass
byte-for-byte.

## Tests Added

`tests/unit/test_database.py`:

- `TestStaleConnectionRecovery::test_connection_string_enables_tcp_keepalive`
- `TestStaleConnectionRecovery::test_connection_string_carries_application_name`
- `TestObservability` (new class):
  - `test_success_log_carries_query_duration_ms`
  - `test_retry_log_carries_attempt_and_duration` (asserts
    `attempt`, `query_duration_ms`, `sql_hash` on the
    "reconnecting and retrying" warning)
  - `test_final_error_log_carries_attempt_duration_sql_hash`
  - `test_sql_hash_is_stable_and_short`

Updated `TestExecuteWithRetry` (4 tests) to the new
`Callable[[Connection], (T, int)]` operation contract and pass
`sql_for_hash=` keyword.

Pre-fix: 6 new tests failed (4 missing fields, 1 missing helper, 1
missing connection-string keyword). Post-fix: all 6 pass.

Full-suite result after fix:

```
174 passed, 3 skipped in 5.53s
```

3 skips are `tests/integration/test_fabric_connection.py` — unchanged
from baseline. `ruff check src/ tests/` clean.

## Spec Compliance

- **`specs/001-fabric-sql-mcp-server/plan.md:31`**: *"Timeout + retry
  with exponential backoff for Fabric connections."* The fix
  strengthens timeout/retry handling at the TCP layer (KeepAlive
  prevents the drop in the first place) but does not introduce
  exponential backoff. The existing one-immediate-retry policy is
  preserved. Carried as Residual Risk #2 from the prior bugfix; not
  re-litigated here.
- **`specs/001-fabric-sql-mcp-server/spec.md:115`, SC-004**: *"Server
  operates continuously for 24+ hours without requiring manual
  reconnection."* Not directly affected (this fix doesn't change
  reconnection logic) but the observability additions make any future
  SC-004 violation diagnosable from a single log line per attempt.

## Residual Risks

1. **Hypothesis-not-yet-validated.** The fix addresses the most likely
   root cause (TCP-idle intermediary drop) per Microsoft documentation,
   but cannot be confirmed against the live network without a
   production deploy + re-run of the failing query. The plan.md
   "Production-validation layer" section enumerates the three possible
   outcomes and what each implies.

2. **If the root cause is server-side Fabric query timeout** rather
   than TCP-idle drop, KeepAlive won't help — Fabric's decision to kill
   long queries is independent of TCP liveness. In that case the
   recommended mitigation is caller-side query splitting (per
   Microsoft Learn's "Split the copy into partitions" guidance). The
   new `query_duration_ms` field will make this distinguishable from
   logs: success at ~20s means TCP drop was the cause; failure at the
   same ~20s with a different SQLSTATE (e.g. HYT00 / a Fabric-specific
   code) means server-side timeout.

3. **App-level retry of mid-query failures is wasted work.** If a
   query consistently dies mid-execution, retrying it once doubles
   user-visible latency. Smarter retry (skip retry if attempt 0 took
   > N seconds) is deferred — it would mask legitimate stale-connection
   recovery, which the prior bugfix added precisely to handle. Revisit
   only if KeepAlive does not eliminate the mid-query failure mode.

4. **English-only message-text fallback in `_is_connection_error`**
   (Residual Risk #3 from the prior bugfix) is unaffected by this fix.
   The container's locale is en_US — no action needed unless that
   changes.

## Git Commits

Not committed yet — pending review. Files staged:

- `src/database.py` — `_sql_hash` helper, connection-string additions,
  `_execute_with_retry` instrumentation, op-signature change
- `tests/unit/test_database.py` — 6 new tests + migrated 4 helper tests
  to the new op signature
- `.claude/bugfix/2026-05-23-fabric-08s01-mid-query-tcp-drop/{repro,plan,report}.md`
