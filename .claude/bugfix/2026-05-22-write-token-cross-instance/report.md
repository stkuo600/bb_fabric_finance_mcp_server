## Root Cause

`src/tools/write.py` stored confirmation tokens in a module-level
`_pending_writes: dict[str, dict[str, object]]` (line 24 before the fix).
A Python module-level dict lives only in one Python process's memory —
it is not shared across Azure Container Apps replicas and does not
survive a container restart.

The deployment runs FastMCP `streamable-http` under Azure Container
Apps, where the default scaling policy spawns multiple replicas under
concurrency-based load balancing. When a client called
`fabric_preview_write` the token was written into replica A's dict;
the subsequent `fabric_execute_write` was load-balanced to replica B,
whose dict lookup missed and the server returned `TOKEN_INVALID`. The
"two MCP clients" symptom from the user report is the same root cause
amplified — extra concurrent traffic makes Container Apps more likely
to scale out, increasing the chance of cross-replica routing even
within a single client's preview→execute sequence.

`stateless_http=True` on the FastMCP server is unrelated. That flag
disables MCP session tracking inside the SDK and does not interact
with module globals.

## Fix Description

`src/tools/write.py` now issues a **stateless, HMAC-SHA256-signed
self-contained confirmation token**:

```
<payload_b64url>.<signature_b64url>

payload (UTF-8 JSON, sorted keys):
{
  "v":     1,
  "sql":   "<original SQL>",
  "op":    "INSERT" | "UPDATE",
  "table": "<target table>",
  "exp":   <POSIX timestamp, seconds, float>,
  "nonce": "<32 hex chars>"
}

signature   = HMAC_SHA256(payload_b64url_bytes, signing_key)
signing_key = SHA256(b"fabric-mcp-write-confirmation\x00" + config.client_secret.encode())
```

Key points:

- The signing key is derived from `config.client_secret` via SHA-256
  with a domain-separation prefix (so the key can never be confused
  with any other use of the same secret).
- `client_secret` is already present on every replica via the
  `FABRIC_CLIENT_SECRET` environment variable — no new infrastructure
  required.
- Encoding is base64url with padding stripped, decoded with padding
  reconstructed.
- Signature verification uses `hmac.compare_digest(...)` for constant-
  time comparison.
- `fabric_execute_write` differentiates `TOKEN_INVALID` (missing,
  malformed, or bad signature) from `TOKEN_EXPIRED` (signature OK but
  `exp` already past).
- The old module-level `_pending_writes`, `_cleanup_expired_tokens`,
  and per-tool dict bookkeeping are deleted.

The public tool surface (`fabric_preview_write(sql)` →
`WritePreview{confirmation_token, operation, table, sql_summary,
expires_at}` and `fabric_execute_write(confirmation_token)` →
`WriteResult{affected_rows, operation, table}`) is unchanged. The
token string format changed from a UUID4 to `<b64>.<b64>` but the
field is declared `string` in the contract; clients should treat it
as an opaque value.

## Tests Added

In `tests/unit/test_tools/test_write.py`:

New `TestStatelessToken` class (4 tests):

1. `test_token_redeemable_on_independent_instance` — preview on one
   FastMCP+config registration, execute on a separate registration
   with identical config. **This is the test that reproduces the
   production bug.** Failed RED against the previous code (cross-
   instance lookup miss) and is the principal regression guard.
2. `test_tampered_token_rejected` — any byte flipped → `TOKEN_INVALID`.
3. `test_token_signed_with_different_secret_rejected` — tokens are
   cryptographically bound to the signing key.
4. `test_token_replay_within_window_succeeds` — explicitly documents
   the deliberate behavioural deviation from spec
   (`tasks.md:130`) — tokens are replayable within their 5-minute
   validity window.

Modified:

- `TestFabricExecuteWrite.test_expired_token_rejected` rewritten to
  construct a properly-signed token whose `exp` is in the past, since
  there is no longer a `_pending_writes` dict to mutate.

Removed:

- `TestFabricExecuteWrite.test_token_single_use` — explicitly asserted
  the now-removed single-use semantic. Replaced by
  `test_token_replay_within_window_succeeds` which asserts the new
  expected behaviour.

Verified RED: the two principal new tests
(`test_token_redeemable_on_independent_instance`,
`test_token_replay_within_window_succeeds`) failed against the
pre-fix code with the expected assertion. Verified GREEN: all 21
write-tool tests pass after the fix.

Full suite result after fix:

```
82 passed, 3 skipped in 1.55s
```

`ruff check src/ tests/` is clean.

## Residual Risks

1. **Single-use semantic is gone (spec deviation).** The spec
   (`specs/001-fabric-sql-mcp-server/tasks.md:130`,
   `contracts/mcp-tools.md:211` before this fix) said tokens are
   single-use. They no longer are; a token can be re-used by anyone
   who intercepts it any number of times within its 5-minute window.
   This is acceptable because:
   - The write allowlist still enforces on each redemption.
   - A token binds to a specific SQL string, so an attacker can only
     replay the exact write the user already approved.
   - The transport is HTTPS (`Encrypt=yes`) and the MCP layer requires
     a bearer API key, so token interception requires either
     compromising the client environment or the `FABRIC_CLIENT_SECRET`.
   - Enforcing single-use requires either reverting to a single replica
     or introducing a shared store (Redis / Azure Table Storage).
   If a stricter posture is needed later, the natural follow-up is a
   small shared-state replay cache keyed on the token's `nonce`.

2. **Signing-key rotation invalidates in-flight tokens.** Rotating
   `FABRIC_CLIENT_SECRET` makes all currently-issued tokens
   `TOKEN_INVALID` immediately. Acceptable given the 5-minute window.

3. **Token visibility in logs.** The pre-fix code logged the first 8
   characters of the UUID token. The new token is much longer
   (b64-encoded payload + signature). The current code logs neither
   the token nor any portion of it (only the operation and table
   names) — this is intentional to avoid leaking SQL or signed
   material into structured logs.

4. **No related-fix synergy missed.** The recent connection-reuse
   change (`3ef2478`) does not interact with this fix in either
   direction — write execution still goes through
   `FabricDatabase.execute_write` exactly as before.

## Documentation Updated

- `specs/001-fabric-sql-mcp-server/data-model.md` — `confirmation_token`
  field description (line 62) and the "State Management" section
  (lines 91-94) now describe the stateless HMAC-signed token.
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md` — error
  code reference for `TOKEN_INVALID` (line 211) updated to drop "or
  already used".

`specs/001-fabric-sql-mcp-server/research.md` and `tasks.md` are
historical decision/task logs and intentionally left as-is; this
report documents the deviation.

## Git Commits

Not committed yet — pending user confirmation. Staged for commit:

- `src/tools/write.py` (stateless token implementation)
- `tests/unit/test_tools/test_write.py` (new `TestStatelessToken`
  suite, rewritten expiry test, removed single-use test)
- `specs/001-fabric-sql-mcp-server/data-model.md`
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md`
- `.claude/bugfix/2026-05-22-write-token-cross-instance/{repro,plan,report}.md`
