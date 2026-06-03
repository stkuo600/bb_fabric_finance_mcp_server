# Plan — Decouple confirmation-token signing key from client_secret

## Root Cause

The token signing key is `sha256(domain + client_secret)`, and `write.py` passes
`config.client_secret` to `make_confirmation_token` / `parse_confirmation_token`. On ACA,
`client_secret` is rotated periodically, so the signing key changes with it — invalidating all
in-flight tokens and breaking cross-replica verification during rolling restarts.

## Proposed Fix

Introduce a dedicated, rotation-stable signing key, independent of the AAD `client_secret`:

1. `config.py`: add optional `token_signing_key: str | None = None` (env `FABRIC_TOKEN_SIGNING_KEY`).
2. `write.py`: resolve the signing material once — `config.token_signing_key or config.client_secret`
   — and pass it (instead of `config.client_secret`) to the three token call sites
   (`make_confirmation_token`, and `parse_confirmation_token` in `fabric_execute_write` and the batch
   tool). Log a one-time warning when falling back to `client_secret`, recommending operators set
   `FABRIC_TOKEN_SIGNING_KEY` so tokens survive AAD-secret rotation.
3. `_confirmation_token.py`: update the module/`_signing_key` docstrings — the key is a dedicated
   signing secret (not the AAD `client_secret`).

Backward compatible: deployments that have not set `FABRIC_TOKEN_SIGNING_KEY` keep working under
`client_secret` (with the warning). Operators opt into rotation-stability by setting the dedicated key,
which they rotate rarely (and independently of the AAD secret).

Out of scope (documented as residual): an accepted-keys overlap list (current + previous) for
zero-downtime rotation of the signing key itself. The decoupling already fixes both reported failure
modes for the common case (AAD-secret rotation / code deploys leave the signing key unchanged).

## Files to Modify

- `src/config.py` — add `token_signing_key` field.
- `src/tools/write.py` — resolve + pass the signing key at the 3 call sites; fallback warning.
- `src/tools/_confirmation_token.py` — docstring wording.
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md` + `quickstart.md` — document
  `FABRIC_TOKEN_SIGNING_KEY`.

## Test Strategy

Failing tests added in `tests/unit/test_tools/test_write.py::TestTokenSigningKeyDecoupledFromClientSecret`:

- `test_token_survives_client_secret_rotation` — token issued under `client_secret=S1` redeems under
  `S2` when both share `token_signing_key` (single + batch variants).
- `test_wrong_signing_key_still_rejected` — different signing keys → `TOKEN_INVALID` (decoupling must
  not weaken verification).
- `test_fallback_to_client_secret_when_no_signing_key` — backward-compat: unset signing key still works
  end-to-end under one `client_secret`.

All existing write / token tests must still pass.
