# Report: pyodbc connection pooling defeats the 08S01 reconnect

## Root Cause

`pyodbc.pooling` was left at its documented default of `True`. When the cached
Fabric connection died (idle disconnect), `_discard_connection()` → `conn.close()`
returned the dead connection to the unixODBC pool instead of closing it, and the
following `_open_connection()` → `pyodbc.connect()` with the identical connection
string drew the **same dead handle** back out of the pool. This silently defeated
the application's existing 08S01 reconnect: every query kept failing `08S01`
indefinitely, and only a full process restart (which empties the pool) recovered
it.

## Fix Description

`src/database.py` — one line plus an explanatory comment, at module scope after
the imports:

```python
pyodbc.pooling = False
```

The app deliberately caches and serialises a single long-lived connection under
`_lock` and recovers from a dead one itself, so driver-level pooling is both
unnecessary and harmful. pyodbc requires `pooling` to be set before the first
connection (it configures the shared HENV); `src/database.py` is imported before
any `FabricDatabase` connects, so module scope is the correct, earliest place.
Verified against pyodbc docs via context7
("`pooling` defaults to True … can only be modified before the first connection
is established").

With pooling off, `_discard_connection()`'s `close()` truly closes the dead
socket and `_open_connection()` performs a genuine fresh handshake with a fresh
token — making the **already-present** 08S01 reconnect work as intended. No
change to `FabricDatabase`, the connection string, the retry policy, or any
caller.

## Tests Added

`tests/unit/test_database.py::TestConnectionPoolingDisabled`:

- `test_module_import_disables_odbc_pooling` — asserts `pyodbc.pooling is False`
  after importing `src.database`.
  - Pre-fix: **FAILED** (`assert True is False` — default `True`).
  - Post-fix: **PASSED**.

No behavioural unit test for the pool's dead-handle reuse: it requires the real
ODBC driver + a real idle disconnect and cannot be reproduced with a mocked
`pyodbc.connect` (mocks return a fresh object per call). Documented in repro.md;
the production reproduction (18 h outage + restart recovery + query-insights gap)
stands as the behavioural evidence.

Full-suite result after fix:

```
239 passed, 3 skipped in 7.17s
```

3 skips are the live-Fabric integration tests (unchanged from baseline).
`ruff check src/ tests/` → `All checks passed!`.

## Spec Compliance

- **`specs/001-fabric-sql-mcp-server/spec.md`, SC-004** — "Server operates
  continuously for 24+ hours without requiring manual … reconnection." The bug
  violated this: after any idle disconnect, every query failed until a manual
  container restart. The prior bugfixes (2026-05-22 IMC06 classification,
  2026-05-23 KeepAlive) both *assumed the reconnect path worked* — pooling
  silently defeated it. This fix restores SC-004 by making that reconnect
  actually rebuild the connection.

## Residual Risks

1. **Healthy-path connection cost.** With pooling off there is no driver-level
   reuse across distinct connection objects, but the app already reuses one
   cached connection for the process lifetime, so steady-state behaviour is
   unchanged. Only genuine reconnects pay a full handshake — which is the
   intended, correct behaviour.
2. **Shallow `/health`.** `/health` still returns 200 while the DB path is
   broken, so it cannot surface this class of failure. A deep health check that
   runs `SELECT 1` would make DB-path outages visible to the platform probe.
   Deferred (min-replicas=0 cost implications); tracked as a follow-up, not part
   of this fix.
3. **Production validation pending.** Unit-level the contract is locked, but the
   end-to-end "idle disconnect now self-heals without restart" can only be
   confirmed after deploy by awaiting/forcing an idle disconnect and observing
   recovery in logs + a corresponding entry in
   `queryinsights.exec_requests_history`.

## Git Commits

- Branch: `fix/pyodbc-pooling-defeats-reconnect`
- `<commit-sha>` fix(db): disable pyodbc ODBC pooling so 08S01 reconnect works
  - `src/database.py` — `pyodbc.pooling = False` + comment
  - `tests/unit/test_database.py` — `TestConnectionPoolingDisabled` guard test
  - `.claude/bugfix/2026-06-09-pyodbc-pooling-defeats-reconnect/{repro,plan,report}.md`
