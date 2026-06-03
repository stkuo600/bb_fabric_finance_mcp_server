# Plan — Stop non-idempotent write retry; make write timeout deterministic

## Root Cause

Under `autocommit=True`, `_execute_with_retry` (and the inline retry in `execute_writes`) treat every
connection-class pyodbc error (`08*`, `IMC*`, `HYT*`) as "the statement never ran" and re-execute the
same write once on a fresh connection. When the drop/timeout fires after the Fabric server committed
but before the driver read the ack, the state is unknown — re-executing double-applies a non-idempotent
INSERT/UPDATE (P1 single, P2 batch). `execute_query` leaves `conn.timeout = 30` on the shared cached
connection; subsequent writes inherit it and can abort with `HYT00`, which is classified as a
connection error and fed into the retry path (P10, amplifier).

## Proposed Fix

Split the retry policy: reads stay retryable (idempotent), writes do not auto-retry on a
connection-class error.

1. `_execute_with_retry(..., retry_on_connection_error: bool = True)`. When `False` and a
   connection-class error occurs, discard the connection and raise `FabricQueryError(code=
   "WRITE_STATE_UNKNOWN", ...)` immediately — the op runs exactly once. Default `True` keeps the read
   path and the existing seam contract unchanged.
2. `execute_write` calls with `retry_on_connection_error=False`.
3. `execute_writes` (batch): on a connection-class error for a statement, do NOT re-execute it; record
   a `WRITE_STATE_UNKNOWN` `FabricQueryError` in that slot, discard the connection, and lazily rebuild
   it for the *next* statement so later statements still run. Query-side errors keep their existing
   "record and continue on the same connection" behavior.
4. P10: set `conn.timeout = 0` at the start of every write op (`execute_write` + `execute_writes`),
   so a write's timeout is deterministic (unbounded, matching the original no-explicit-write-timeout
   intent) and never inherits a prior query's residual — also removing the spurious `HYT00`-on-write
   that fed the retry path.

`WRITE_STATE_UNKNOWN` carries a message telling the caller the write may or may not have committed and
to verify before retrying.

Why no-retry over idempotency keys / MERGE: making writes idempotent is a much larger change (token
format + server-side dedup). The deployment already has driver-level connection resiliency
(`ConnectRetryCount=3`), so the common idle-disconnect is handled transparently before reaching this
path; surfacing the rare ambiguous mid-flight drop as `WRITE_STATE_UNKNOWN` is the minimal correct fix.

## Files to Modify

- `src/database.py` — `_execute_with_retry` (add flag + unknown-state raise), `execute_write`
  (flag + `conn.timeout = 0`), `execute_writes` (no-retry-for-writes loop + `conn.timeout = 0`).
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md` — document the `WRITE_STATE_UNKNOWN` error
  code (spec-compliance).

## Test Strategy

Failing tests added/updated in `tests/unit/test_database.py`:

- `TestWriteRetrySafety` — write does NOT retry on 08S01 / HYT00 (op runs once, raises
  `WRITE_STATE_UNKNOWN`); dropped connection is discarded; **reads still reconnect-and-retry**
  (regression guard); write resets residual query timeout to 0; seam test for
  `retry_on_connection_error=False`.
- `test_execute_writes_connection_error_mid_batch_does_not_retry_failed_stmt` and
  `test_execute_writes_connection_error_flags_unknown_state_no_retry` — rewrites of the two prior
  tests that locked in the unsafe retry-the-failed-statement behavior.

All other pre-existing database tests must continue to pass (success paths, query-side error handling,
connection reuse, observability, classifier).
