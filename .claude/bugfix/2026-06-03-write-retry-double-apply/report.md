# Report — Non-idempotent write retry double-applies writes (P1 / P2 / P10)

## Root Cause

Under `autocommit=True`, `_execute_with_retry` (and the inline retry in `execute_writes`) treated
every connection-class pyodbc error (`08*`, `IMC*`, `HYT*`) as "the statement never ran" and
re-executed the same write once on a fresh connection. When a mid-flight drop (`08S01`) or client
timeout (`HYT00`) fires *after* the Fabric server committed but before the driver reads the ack, the
outcome is unknown — re-executing double-applies a non-idempotent INSERT/UPDATE (P1 single, P2 batch).
`execute_query` also left `conn.timeout = 30` on the shared cached connection; subsequent writes
inherited it and could abort with `HYT00`, feeding the retry path (P10, amplifier).

## Fix Description

Split the retry policy — reads stay retryable (idempotent), writes do not auto-retry on a
connection-class error:

1. `_execute_with_retry(..., retry_on_connection_error: bool = True)`. With `False`, a connection-class
   error discards the connection and raises `FabricQueryError(code="WRITE_STATE_UNKNOWN", ...)`
   immediately — the op runs exactly once. Default `True` leaves the read path and the existing seam
   contract unchanged.
2. `execute_write` passes `retry_on_connection_error=False`.
3. `execute_writes` (batch): a connection-class error on a statement records `WRITE_STATE_UNKNOWN` for
   that slot, discards the connection, and rebuilds it lazily (`cursor = None`) for the *next*
   statement — the failed statement is never re-executed, later statements still run. Query-side errors
   keep the connection and continue as before.
4. P10: every write op sets `conn.timeout = 0` (deterministic, unbounded — matching the original
   no-explicit-write-timeout intent), so a write never inherits a prior query's residual timeout and
   the spurious `HYT00`-on-write that fed the retry path is removed.

`WRITE_STATE_UNKNOWN` carries a message telling the caller the write may or may not have committed and
to verify before retrying. It is documented in the spec contract's Error Code Reference.

Why no-retry rather than idempotency keys / MERGE: full idempotency is a much larger change (token
format + server-side dedup). The deployment already has driver-level resiliency (`ConnectRetryCount=3`)
that transparently handles the common idle-disconnect before this path, so surfacing the rare ambiguous
mid-flight drop as `WRITE_STATE_UNKNOWN` is the minimal correct fix.

## Tests Added

In `tests/unit/test_database.py`:

- `TestWriteRetrySafety` — write does NOT retry on `08S01` / `HYT00` (op runs once → `WRITE_STATE_UNKNOWN`);
  dropped connection is discarded; **reads still reconnect-and-retry** (regression guard); write resets
  the residual query timeout to 0 (P10); seam test for `retry_on_connection_error=False`.
- Rewrote `test_execute_writes_connection_error_mid_batch_does_not_retry_failed_stmt` and
  `test_execute_writes_connection_error_flags_unknown_state_no_retry` — these replace the two prior
  tests that asserted (and thus locked in) the unsafe retry-the-failed-statement behavior.

Verification:
- New/updated tests failed before the fix, pass after.
- `tests/unit/test_database.py`: 48 passed. Full suite: **201 passed, 6 skipped** (pre-existing async skips).
- `ruff check` on changed files: clean.

## Residual Risks

- **Tiny ambiguous window remains for legitimate transient drops:** a write whose connection died
  *before* the statement was sent (e.g. idle disconnect not caught by driver resiliency) now surfaces
  `WRITE_STATE_UNKNOWN` even though nothing committed — a false alarm that errs on the safe side
  (caller verifies instead of silently duplicating). Driver-level `ConnectRetryCount` keeps this rare.
- **Not full idempotency:** concurrent duplicate writes from other sources, or replays of confirmation
  tokens across replicas, are out of scope. True protection would be a client idempotency key /
  `MERGE` on a natural key — tracked as a larger follow-up alongside the stateless-token redesign.
- **Unbounded write timeout (`conn.timeout = 0`):** a genuinely hung write relies on TCP KeepAlive
  (`KeepAlive=10`) to eventually surface a connection error rather than a client-side timeout. This
  matches the original intent; a dedicated bounded write timeout could be a future enhancement.
- `WRITE_STATE_UNKNOWN` is a new error code; callers/LLMs that branch on specific codes should learn to
  treat it as "verify then decide", not "safe to blindly retry".

## Git Commits

- Branch: `fix/write-retry-unknown-state` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
