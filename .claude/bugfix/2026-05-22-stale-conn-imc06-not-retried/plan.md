## Root Cause

`FabricDatabase._is_connection_error` (`src/database.py:30-38`) gates the
reconnect-and-retry path on `exc.args[0].startswith("08")` — the ISO SQL
"connection exception" class. Fabric / Microsoft ODBC Driver 18 reports
idle-disconnect failures with several **other** SQLSTATEs that bypass
this filter:

- **`HY000`** ("General error") — the generic wrapper the driver emits
  when the server-side terminates the TCP connection. Carries messages
  like `Communication link failure` or `[TCP Provider] Error code 0x2746
  (10054)` (WSAECONNRESET).
- **`IMC06`** — Microsoft-specific SQLSTATE meaning *"The connection is
  broken and recovery is not possible. The connection is marked by the
  client driver as unrecoverable. **No attempt was made to restore the
  connection.**"* (Microsoft Learn: *Connection resiliency in the ODBC
  driver*.) Critically: once the driver sets this marker, every
  subsequent `cursor.execute()` on the same connection returns `IMC06`
  immediately with no round trip — explaining why the production symptom
  is *permanent* breakage, not a one-shot failure.
- **`IMC01`–`IMC05`** — sibling driver-resiliency error states.
- **`HYT00` / `HYT01`** — connection / query timeout. After a timeout
  the connection state is suspect and reuse is unsafe.

When the first stale-connection query happens to raise one of these
(rather than `08S01`), `_is_connection_error` returns False, the retry
loop skips the discard branch, and `self._conn` retains the dead
pyodbc.Connection object indefinitely. The next call reuses it, fails
identically (now with `IMC06` because the driver has set its marker),
and the loop is closed — container restart is the only escape.

There is also a *prevention* gap: the connection string omits
`ConnectRetryCount` / `ConnectRetryInterval`, so the driver's built-in
idle-connection resiliency is disabled. Per Microsoft Learn the feature
is supported on Fabric SQL database and would silently reconnect most
cases before the application ever sees the error.

## Proposed Fix

Two-layer defense, matching the user's suggestions #1 and #2:

### Layer 1 — Driver-level resiliency (prevention)

Add `ConnectRetryCount=3;ConnectRetryInterval=10` to the connection
string. The ODBC driver will detect a broken idle connection on the
next `cursor.execute()` and silently re-establish it (up to 3 attempts,
10 s apart) before propagating any error to the application. This
handles the common case transparently and eliminates the user-visible
extra round trip.

Cost: zero — `ConnectRetryCount` is purely opt-in driver behaviour with
no overhead on the healthy path. Compatible with `autocommit=True`
sessions (no transaction state to restore). Fabric SQL database supports
the feature (Microsoft Learn confirmed).

### Layer 2 — Broaden application classifier (fallback)

Rewrite `_is_connection_error` to recognise the full set of SQLSTATEs
that signal a dead/suspect connection:

- `08*` — ISO connection exception (unchanged)
- `IMC*` — Microsoft client-marked unrecoverable
- `HYT*` — connection / query timeout

Plus a message-text fallback for cases where the SQLSTATE is generic
(`HY000`) but the message contains a known connection-failure phrase:

- `"communication link"`
- `"tcp provider"`
- `"connection is broken"`
- `"connection forcibly closed"`
- `"server has terminated the connection"`

These phrases appear verbatim in the Microsoft ODBC driver's English
diagnostic strings. Limiting the substring match to these specific
phrases (vs. matching all of `HY000`) preserves the existing behaviour
for genuine query-side errors that happen to use `HY000`.

The retry policy itself (one immediate retry after discard) is
unchanged. Driver-level retry adds up to 3 reconnect attempts inside
that one application-level attempt, so the total budget is 4 reconnects
worst case — adequate for transient infra blips, bounded so a truly
dead endpoint surfaces an error quickly.

## Files to Modify

- `src/database.py`
  - `FabricDatabase.__init__`: append `ConnectRetryCount=3;ConnectRetryInterval=10`
    to `self._connection_string`.
  - `_is_connection_error`: rewrite per Layer 2 above.

## Test Strategy

Already RED (commit-staged in `tests/unit/test_database.py::TestStaleConnectionRecovery`):

1. `test_reconnects_on_imc06_sqlstate` — pyodbc.Error("IMC06", …) → reconnects.
2. `test_reconnects_on_imc01_sqlstate` — pyodbc.Error("IMC01", …) → reconnects.
3. `test_reconnects_on_hy000_with_communication_link_failure` — HY000 +
   "Communication link failure" message → reconnects.
4. `test_reconnects_on_hy000_with_tcp_provider_reset` — HY000 + "[TCP
   Provider] Error code 0x2746" (the exact user-reported chain) → reconnects.
5. `test_reconnects_on_hyt00_connection_timeout` — HYT00 → reconnects.
6. `test_write_reconnects_on_imc06` — same broadening applies to `execute_write`.
7. `test_genuine_query_error_still_does_not_reconnect` — regression
   guard: 42S02 must NOT reconnect (must already pass pre-fix, continue
   passing post-fix).
8. `test_connection_string_enables_idle_resiliency` — connection string
   contains `ConnectRetryCount` and `ConnectRetryInterval`.

Pre-fix run: 7 failed, 1 passed (the regression guard). Post-fix
expectation: 8 passed plus all `TestConnectionReuse` and `TestFabricDatabase`
tests continue to pass.

Stable network-level reproduction is not feasible (requires Fabric-side
idle timeout — see `repro.md`); the unit tests above target the exact
defective predicate and the exact code path, which is the actual root
cause of the permanent-breakage half of the symptom.
