# Plan — Scrub MSAL/AADSTS detail from client-facing auth errors

## Root Cause

`FabricAuth.get_token` builds the failure `ErrorResponse.message` from `result["error_description"]` and
`details` from `result["error"]`, then raises `RuntimeError(model_dump_json())`, which propagates to the
client carrying raw AADSTS text and trace/correlation IDs.

## Proposed Fix

Log server-side, scrub client-side. On failure:
- `logger.error(...)` the full MSAL `error` + `error_description` server-side (trusted logs).
- Raise `RuntimeError` with a generic client-facing `ErrorResponse(code="AUTH_FAILED", message=
  "Authentication to the upstream identity provider failed.", details=None)` — no AADSTS text, no IDs.

Keeps the stable `AUTH_FAILED` code that callers/tests rely on. Minimal change in `src/auth.py`.

## Files to Modify

- `src/auth.py` — `get_token` failure path.

## Test Strategy

- `test_failure_does_not_leak_aadsts_detail_to_client` — raised error has `AUTH_FAILED`, no AADSTS code,
  no trace/correlation IDs.
- `test_failure_logs_full_msal_detail_server_side` — full MSAL detail retained in server logs.
- Existing `test_get_token_raises_on_failure` (matches `AUTH_FAILED`) must still pass.
