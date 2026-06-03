# Report — Auth failure leaks MSAL/AADSTS detail to client (P12, Low)

## Root Cause

`FabricAuth.get_token` built the failure `ErrorResponse` from `result["error_description"]` /
`result["error"]` and raised `RuntimeError(model_dump_json())`, which propagates to the MCP client
carrying raw AADSTS text and trace/correlation IDs — reconnaissance-grade identity disclosure.

## Fix Description

Log server-side, scrub client-side. On failure, `get_token` now logs the full MSAL
`error_description` + `error` at ERROR level server-side, and raises a `RuntimeError` carrying a generic
`ErrorResponse(code="AUTH_FAILED", message="Authentication to the upstream identity provider failed.",
details=None)`. The stable `AUTH_FAILED` code is preserved for callers; no AADSTS text or IDs reach the
client. `client_secret` was never in the message; this closes the AADSTS/ID leak.

Scope: `src/auth.py` only.

## Tests Added

In `tests/unit/test_auth.py::TestFabricAuth`:

- `test_failure_does_not_leak_aadsts_detail_to_client` — raised error has `AUTH_FAILED`, no AADSTS code,
  no trace/correlation IDs.
- `test_failure_logs_full_msal_detail_server_side` — full MSAL detail retained in server logs.

Verification: both failed before, pass after; existing `test_get_token_raises_on_failure` (matches
`AUTH_FAILED`) still passes. auth suite: 7 passed. Full suite: **197 passed, 6 skipped**. `ruff` clean.

## Residual Risks

- Triggered only on auth-acquisition failure (e.g. secret expiry/rotation), not normal traffic. The
  generic client message is intentionally non-actionable; operators diagnose via server logs.
- Any future code that surfaces MSAL results to the client should follow the same log-server /
  scrub-client convention.

## Git Commits

- Branch: `fix/auth-error-scrub` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
