## Root Cause

Confirmation tokens are stored in `_pending_writes: dict[str, dict[str, object]]`
at module level in `src/tools/write.py:24`. A Python module-level dict is
process-local — it is **not** shared across Container Apps replicas, nor
does it survive a container restart. `fabric_preview_write` writes into
the replica-local dict; `fabric_execute_write` reads from the replica-local
dict; if the L7 load balancer routes the second call to a different
replica, the dict lookup misses and the server returns `TOKEN_INVALID`.

`stateless_http=True` is unrelated — it only disables MCP session
tracking inside FastMCP and does not interact with module globals.

## Proposed Fix

Replace process-local storage with a **stateless self-contained
confirmation token** that any replica sharing the same configuration
can verify. The token format is:

```
<payload_b64url>.<signature_b64url>

payload (UTF-8 JSON):
{
  "v": 1,
  "sql": "<original SQL>",
  "op": "INSERT" | "UPDATE",
  "table": "<target table>",
  "exp": <POSIX timestamp, seconds, float>,
  "nonce": "<32 hex chars from secrets.token_hex(16)>"
}

signature = HMAC-SHA256(payload_b64url_bytes, signing_key)
signing_key = SHA256(b"fabric-mcp-write-confirmation\x00" + config.client_secret.encode())
```

Notes:
- `signing_key` is derived from `config.client_secret` via SHA-256
  with a domain-separation prefix. `client_secret` is already present
  on every replica via the `FABRIC_CLIENT_SECRET` env var, so no new
  infrastructure or shared store is needed.
- Base64url, no padding, via `base64.urlsafe_b64encode(...).rstrip(b"=")`.
- Signature verification uses `hmac.compare_digest(...)` for constant
  time comparison.
- `nonce` is included for token uniqueness on the wire but is **not**
  enforced server-side (would require a shared replay cache).
- `op` is short for `operation`; the public response shape is unchanged.

### Behavioural change: single-use → replayable within window

The original spec (`specs/001-fabric-sql-mcp-server/tasks.md:130`,
`contracts/mcp-tools.md:211`) specifies one-time use of the confirmation
token. With stateless tokens there is no place to store "this token has
been used" without re-introducing the cross-replica problem we are
fixing. **A token therefore becomes replayable within its 5-minute
validity window.**

This is acceptable because:
1. The write allowlist still applies on every redemption.
2. The token is bound to a specific SQL string; an attacker who steals
   a token can only re-issue the same write the user already approved.
3. The 5-minute window bounds the replay surface.
4. The alternative (per-token shared store) requires new infra (Redis
   / Azure Table Storage) which is out of scope for the immediate fix.

This deviation will be documented in `data-model.md`,
`contracts/mcp-tools.md`, `research.md`, and the bug-fix `report.md`.

## Files to Modify

- `src/tools/write.py`
  - Remove `_pending_writes`, `_cleanup_expired_tokens`,
    `_TOKEN_EXPIRY_MINUTES` becomes a constant used by the new helpers.
  - Add `_signing_key(secret) -> bytes`, `_b64u_encode(bytes) -> str`,
    `_b64u_decode(str) -> bytes`, `_make_token(payload, secret) -> str`,
    `_verify_token(token, secret) -> dict | None`.
  - `fabric_preview_write`: build payload, sign, return token string.
  - `fabric_execute_write`: verify signature → check `exp` → execute.
    Differentiates `TOKEN_INVALID` (signature/format failure) from
    `TOKEN_EXPIRED` (signature OK but past `exp`).

- `tests/unit/test_tools/test_write.py`
  - Drop the `_pending_writes` import and `setup_method` clears.
  - Delete `test_token_single_use` (asserts the now-removed semantic).
  - Rewrite `test_expired_token_rejected` to construct an expired
    token directly (no `_pending_writes` mutation).
  - Keep the new `TestStatelessToken` suite added in the RED phase.

- `specs/001-fabric-sql-mcp-server/data-model.md`
  - Line 62: token format description ("UUID" → "signed self-contained")
  - Lines 91-94: "Confirmation tokens" storage note → describe stateless
    HMAC-signed approach.

- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md`
  - Line 211: `TOKEN_INVALID` description: drop "or already used"
    (no longer applicable).

## Test Strategy

The `TestStatelessToken` suite already written in the RED phase covers:

1. `test_token_redeemable_on_independent_instance` — preview on one
   FastMCP registration, clear shared state, execute on another
   registration with same config; must succeed. **This is the test
   that reproduces the production bug.**
2. `test_tampered_token_rejected` — any byte flipped in the token
   surfaces as `TOKEN_INVALID` (signature check fails).
3. `test_token_signed_with_different_secret_rejected` — tokens are
   bound to the signing key.
4. `test_token_replay_within_window_succeeds` — documents the
   deliberate behavioural deviation.

Existing tests remain in place except for the deletions / rewrites in
the section above. The contract test
`test_execute_requires_confirmation_token` (a JSON-schema check on the
tool signature) is unaffected since the public parameter name and type
do not change.
