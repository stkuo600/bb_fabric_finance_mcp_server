# Report — Confirmation-token signing key decoupled from client_secret (P6, High)

## Root Cause

The confirmation-token HMAC signing key was `sha256(domain + client_secret)`, and `write.py` passed
`config.client_secret` to `make_confirmation_token` / `parse_confirmation_token`. On Azure Container
Apps the AAD `client_secret` is rotated periodically, so the signing key changed with it — invalidating
every in-flight token (TTL up to 15 min) and breaking cross-replica verification during rolling
restarts (old-secret and new-secret replicas could not verify each other's tokens).

## Fix Description

Introduced a dedicated, rotation-stable signing key independent of `client_secret`:

1. `config.py`: added optional `token_signing_key: str | None = None` (env `FABRIC_TOKEN_SIGNING_KEY`).
2. `write.py`: a `_token_signing_key(config)` helper resolves the signing material as
   `config.token_signing_key or config.client_secret`, used at all three token call sites
   (`make_confirmation_token`, and `parse_confirmation_token` in `fabric_execute_write` and
   `fabric_execute_write_batch`). When falling back to `client_secret` it logs a warning recommending
   `FABRIC_TOKEN_SIGNING_KEY`.
3. `_confirmation_token.py`: docstring updated — the key is a dedicated signing secret, not the AAD
   `client_secret`.
4. Documented `FABRIC_TOKEN_SIGNING_KEY` in the spec contract and quickstart.

Backward compatible: deployments without `FABRIC_TOKEN_SIGNING_KEY` keep working under `client_secret`
(with the warning). Operators opt into rotation-stability by setting the dedicated key (shared across
replicas, rotated rarely and independently of the AAD secret).

Scope: `src/config.py`, `src/tools/write.py`, `src/tools/_confirmation_token.py` (docstring), and two
spec docs.

## Tests Added

In `tests/unit/test_tools/test_write.py::TestTokenSigningKeyDecoupledFromClientSecret`:

- `test_token_survives_client_secret_rotation` / `test_batch_token_survives_client_secret_rotation` —
  a token issued under `client_secret=S1` redeems under `S2` when both share `token_signing_key`
  (single + batch tools).
- `test_wrong_signing_key_still_rejected` — different signing keys → `TOKEN_INVALID` (verification not
  weakened).
- `test_fallback_to_client_secret_when_no_signing_key` — backward-compat: unset key still works
  end-to-end under one `client_secret`.

Verification:
- The rotation/wrong-key tests failed before the fix, pass after; fallback test passed throughout.
- write/config/token suites: 88 passed. Full suite: **199 passed, 6 skipped**.
- `ruff check` on changed files: clean.

## Residual Risks

- **No overlap (current + previous) accepted-keys list:** rotating the dedicated
  `FABRIC_TOKEN_SIGNING_KEY` itself still invalidates in-flight tokens during that rotation window.
  This is rare (the dedicated key need not rotate when the AAD secret does), but a future enhancement
  could accept a previous key during a grace window for zero-downtime signing-key rotation.
- **Fallback still vulnerable:** deployments that never set `FABRIC_TOKEN_SIGNING_KEY` retain the old
  behavior. The warning surfaces this; consider making the key required in a future major version.
- The stateless replay-within-window trade-off documented in
  `.claude/bugfix/2026-05-22-write-token-cross-instance` is unchanged by this fix.

## Git Commits

- Branch: `fix/token-signing-key-decouple` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
