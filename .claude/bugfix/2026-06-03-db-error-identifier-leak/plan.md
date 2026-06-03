# Plan — Scrub server/database identifiers from client-facing DB error messages

## Root Cause

`_execute_with_retry` (and `execute_writes`) raise `FabricQueryError(message=str(e))`, and ODBC/Fabric
diagnostics embed the Fabric server FQDN and database name. `error_envelope` returns that verbatim to
the client, disclosing backend identifiers.

## Proposed Fix

Log server-side, scrub client-side:

1. Add `FabricDatabase._redact_identifiers(text) -> str` that replaces occurrences of `self._server`
   and `self._database` with `<redacted>`. (Targeted redaction preserves useful query-side diagnostics
   like "Invalid object name 'foo'" / syntax errors that help the caller self-correct, and keeps all
   existing message-content tests passing.)
2. In both error-raise sites (`_execute_with_retry`, `execute_writes`): log the full `str(e)`
   server-side via a new `error_detail` field on the existing error log, and raise the
   `FabricQueryError` with `message = self._redact_identifiers(str(e))`. The Fabric hint is computed
   from the redacted message (its keyword detection is unaffected by identifier redaction).

Minimal, no signature/contract changes; `sqlstate` and the Fabric hint still flow to the caller.

## Files to Modify

- `src/database.py` — add `_redact_identifiers`; redact the raised message + log `error_detail` in the
  two raise sites.

## Test Strategy

- `test_error_message_redacts_server_and_database_identifiers` — raised message excludes host + db,
  keeps `sqlstate`.
- `test_error_message_preserves_query_side_text` — "Invalid object name 'foo'" preserved.
- `test_full_error_text_logged_server_side` — full raw text retained server-side via `error_detail`.

Existing error tests (typed fields, Fabric hints, classifier) must still pass.
