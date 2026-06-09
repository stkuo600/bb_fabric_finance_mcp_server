# Plan

## Root Cause

`pyodbc.pooling` is left at its documented default of `True`, so when the
cached connection dies and `_discard_connection()` calls `conn.close()`, the
dead connection is returned to the unixODBC pool rather than closed; the
following `_open_connection()` → `pyodbc.connect()` with the identical
connection string draws the **same dead handle** back out of the pool, which
silently defeats the existing 08S01 reconnect — every query then fails
`08S01` until the process is restarted (the only thing that empties the pool).

## Proposed Fix

Disable ODBC connection pooling at import time of `src/database.py`, before any
connection is established. The app deliberately manages its own single
long-lived connection serialised by `_lock`, so driver-level pooling is
unnecessary and actively harmful here. pyodbc requires `pooling` to be set
before the first connect (it configures the shared HENV), and `database.py` is
imported before any `FabricDatabase` connects, so module scope is the correct
and earliest place.

```python
import pyodbc

# This app caches and serialises a single connection itself and recovers from
# a dead one via _discard_connection() + reconnect. ODBC pooling (pyodbc's
# default) breaks that recovery: close() returns the dead connection to the
# pool and the next connect() with the same connection string hands it back,
# so the 08S01 reconnect reuses the dead handle forever (only a process
# restart clears the pool). Must be set before the first connect — pyodbc docs.
# See .claude/bugfix/2026-06-09-pyodbc-pooling-defeats-reconnect.
pyodbc.pooling = False
```

No change to `FabricDatabase`, the connection string, the retry policy, or any
caller. Behaviour change is confined to making `close()` actually close, which
makes the already-present reconnect work as intended.

## Files to Modify

- `src/database.py` — add `pyodbc.pooling = False` (with explanatory comment)
  at module scope, immediately after `import pyodbc`.
- `tests/unit/test_database.py` — `TestConnectionPoolingDisabled` guard test
  (already added, currently failing).

## Test Strategy

- **Failing guard test (added):**
  `TestConnectionPoolingDisabled::test_module_import_disables_odbc_pooling`
  asserts `pyodbc.pooling is False` after importing `src.database`. Fails on
  current code (default `True`), passes after the fix.
- **No behavioural unit test** for the pool's dead-handle reuse — it requires
  the real ODBC driver + a real idle disconnect and cannot be faked with a
  mocked `pyodbc.connect` (documented in repro.md).
- **Regression:** full `pytest` suite must stay green — the fix is a global
  flag and must not perturb the existing mocked-`connect` tests.
- **Production validation (post-deploy):** after deploying, force/await an idle
  disconnect and confirm the next query self-recovers (logs show
  `reconnecting` → `Connected` → success) without a manual restart, and that
  the query appears in `queryinsights.exec_requests_history`.
