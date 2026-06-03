# Report — DB error messages leak server/database identifiers (P9, Medium)

## Root Cause

`_execute_with_retry` and `execute_writes` raised `FabricQueryError(message=str(e))`, and ODBC/Fabric
diagnostics embed the Fabric server FQDN and database name. `error_envelope` returned that verbatim to
the (authenticated) MCP client — reconnaissance-grade disclosure of backend identifiers.

## Fix Description

Log server-side, scrub client-side. Added `FabricDatabase._redact_identifiers(text)` which replaces
occurrences of `self._server` and `self._database` with `<redacted>`. Both error-raise sites now log the
full raw error server-side via a new `error_detail` log field and raise the `FabricQueryError` with the
identifier-scrubbed message. Targeted redaction preserves useful query-side diagnostics (e.g. "Invalid
object name 'foo'", syntax errors) that help the caller self-correct, and keeps `sqlstate` + the Fabric
hint flowing. The access token never appeared in messages (it is passed via a connection attribute), so
no secret was leaked — only host/db identifiers, now redacted.

Scope: `src/database.py` only. No signature/contract change.

## Tests Added

In `tests/unit/test_database.py::TestFabricDatabase`:

- `test_error_message_redacts_server_and_database_identifiers` — raised message excludes host + db,
  `sqlstate` preserved.
- `test_error_message_preserves_query_side_text` — "Invalid object name 'foo'" preserved.
- `test_full_error_text_logged_server_side` — full raw text retained server-side via `error_detail`.

Verification: the redact + server-log tests failed before, pass after; the preserve test passed
throughout. db suite: 45 passed. Full suite: **198 passed, 6 skipped**. `ruff` clean.

## Residual Risks

- Redaction is exact-substring on `self._server` / `self._database`. If a driver ever emits a host in a
  different form (e.g. resolved IP, or the bare host without the domain suffix), that variant would not
  be caught. The common ODBC text embeds the configured FQDN, which is covered. A future enhancement
  could also redact a generic `*.datawarehouse.fabric.microsoft.com` pattern and private IPs.
- Query-side messages are intentionally preserved for LLM self-correction; these are not considered
  sensitive (they describe the caller's own SQL/object names, not backend topology).

## Git Commits

- Branch: `fix/scrub-db-error-identifiers` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
