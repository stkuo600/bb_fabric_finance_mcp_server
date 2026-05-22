## Root Cause

`FabricDatabase._is_connection_error` (`src/database.py:30-38`) only
recognised SQLSTATEs starting with `08*` as connection failures. Three
other classes of SQLSTATE that the Microsoft ODBC Driver 18 emits on
Fabric idle disconnect were misclassified as query-side errors:

- `HY000` with TCP / communication-link messages — the generic wrapper
  the driver uses when Fabric terminates an idle TCP connection.
- `IMC*` — Microsoft's connection-resiliency error states. `IMC06` in
  particular means the driver has marked the connection unrecoverable
  and *will not attempt recovery on any future operation*; every
  subsequent `cursor.execute` returns `IMC06` instantly with no round
  trip. (Microsoft Learn: *Connection resiliency in the ODBC driver*.)
- `HYT*` — connection / query timeout.

When the first stale-connection failure carried one of these SQLSTATEs,
the reconnect-and-retry branch was skipped, `self._conn` retained the
dead pyodbc.Connection, and every subsequent tool call reused it —
producing the user-reported "every query returns IMC06, only container
restart fixes it" loop.

Compounding this, the connection string did not enable driver-level
idle resiliency (`ConnectRetryCount` / `ConnectRetryInterval`), so the
ODBC driver took no action of its own before propagating the error.

## Fix Description

`src/database.py` — two edits, both local:

1. **Connection string** (`__init__`): append
   `ConnectRetryCount=3;ConnectRetryInterval=10`. The driver detects a
   broken idle connection on the next operation and silently reconnects
   (up to 3 attempts, 10 s apart) before surfacing any error. Supported
   on Fabric SQL database. Compatible with the existing
   `autocommit=True` session. Zero overhead on the healthy path.

2. **`_is_connection_error`**: replaced the single `startswith("08")`
   gate with:
   - SQLSTATE prefix match against `("08", "IMC", "HYT")`
   - Plus a narrow message-text fallback for `HY000`-class wrappers,
     matching the specific phrases `"communication link"`, `"tcp provider"`,
     `"connection is broken"`, `"connection forcibly closed"`,
     `"server has terminated the connection"`.

   The two new module-level constants (`_CONNECTION_SQLSTATE_PREFIXES`,
   `_CONNECTION_MESSAGE_FRAGMENTS`) keep the lists explicit and grep-able.

The retry policy itself is unchanged: one immediate application-level
retry after discarding the dead connection. Combined with the driver's
3 internal reconnect attempts, the worst-case failure budget is
4 reconnects — bounded so a genuinely dead endpoint surfaces quickly.

Caller-visible surface (`src/tools/*.py`) is unchanged: same return
types, same `QUERY_ERROR` code, same `_hint_for_fabric_error` path on
non-connection errors.

## Tests Added

8 new tests in `tests/unit/test_database.py::TestStaleConnectionRecovery`:

1. `test_reconnects_on_imc06_sqlstate` — IMC06 → discard + reconnect.
2. `test_reconnects_on_imc01_sqlstate` — IMC01 (driver resiliency
   exhausted) → discard + reconnect.
3. `test_reconnects_on_hy000_with_communication_link_failure` — HY000
   message-text fallback for "Communication link failure".
4. `test_reconnects_on_hy000_with_tcp_provider_reset` — reproduces the
   exact user-reported error chain ("[TCP Provider] Error code 0x2746").
5. `test_reconnects_on_hyt00_connection_timeout` — HYT00 → reconnect.
6. `test_write_reconnects_on_imc06` — broadening also covers
   `execute_write`.
7. `test_genuine_query_error_still_does_not_reconnect` — regression
   guard: `42S02` invalid object name must NOT reconnect.
8. `test_connection_string_enables_idle_resiliency` — connection string
   contains `ConnectRetryCount` and `ConnectRetryInterval`.

Pre-fix: 7 failed, 1 passed (the regression guard, as designed).
Post-fix: 8 passed.

Full-suite result after fix:

```
133 passed, 3 skipped in 5.70s
```

The 3 skips are `tests/integration/test_fabric_connection.py` which
require live Fabric credentials and are skipped without them — unchanged
from baseline. `ruff check src/ tests/` reports `All checks passed!`.

## Spec Compliance

- **`specs/001-fabric-sql-mcp-server/spec.md:115`, SC-004**: *"Server
  operates continuously for 24+ hours without requiring manual token
  refresh or reconnection."* Pre-fix the server violated this whenever
  Fabric dropped the idle TCP connection — every subsequent call
  returned IMC06 until manual container restart. The fix restores
  SC-004 by (a) silently reconnecting at driver level and (b) discarding
  + rebuilding the cached connection when driver-level recovery fails.
- **`specs/001-fabric-sql-mcp-server/plan.md:31`**: *"Timeout + retry
  with exponential backoff for Fabric connections."* The fix is a step
  toward this — 4-reconnect total budget across driver + application
  layers — but the intervals are fixed (driver) and zero (application
  retry), not exponential. Carried over as Residual Risk #2 below.

## Residual Risks

1. **One stuck call remains possible.** When driver-level resiliency
   has not been triggered yet (first idle disconnect after start, or
   after `_open_connection` returns a fresh socket that immediately
   becomes stale), the application path still pays one failed
   `cursor.execute` round trip before the discard-and-retry. The
   user-visible call still succeeds, but slightly slower than the
   purely driver-handled case.

2. **No exponential backoff.** Per spec/plan, the eventual policy
   should be exponential. Current behaviour: driver retries 3× at fixed
   10 s, then application retries 1× immediately. Acceptable for
   single-instance interactive LLM workloads; revisit if production
   shows repeated transient bursts.

3. **HY000 message-text matching is English-only.** The fallback
   phrases assume the MS-ODBC driver's default English diagnostic
   messages. The driver respects the system locale; a deployment with a
   non-English locale would lose this fallback path. The SQLSTATE
   prefixes (`08*`, `IMC*`, `HYT*`) are locale-independent and remain
   correct, so this only affects the narrow "HY000 with generic
   wrapper" case. Container Apps deployments default to en-US; no
   action needed unless that changes.

4. **Token refresh on long-lived sessions.** Unchanged from the prior
   bugfix. The access token is bound at connect time. Driver-level
   reconnect after idle disconnect reuses the cached token; if the
   token has also expired, the application-level retry path will pick
   up a fresh token via `FabricAuth.get_token()` on the next
   `_open_connection`.

## Git Commits

Not committed yet — pending user confirmation. Files changed:

- `src/database.py`
  - connection string: `+ ConnectRetryCount=3;ConnectRetryInterval=10`
  - `_is_connection_error`: broadened SQLSTATE prefix set + message-text fallback
- `tests/unit/test_database.py`: 8 new tests in `TestStaleConnectionRecovery`
- `.claude/bugfix/2026-05-22-stale-conn-imc06-not-retried/{repro,plan,report}.md`
