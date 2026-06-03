# Repro — Confirmation-token signing key derived from client_secret breaks on rotation

## Symptom

The confirmation-token HMAC signing key is `sha256(domain + client_secret)`
(`_confirmation_token._signing_key`), and `write.py` passes `config.client_secret` to both
`make_confirmation_token` and `parse_confirmation_token`. On Azure Container Apps, `client_secret` is
an injected secret env var that is rotated periodically. Two failure modes:

1. **After rotation**, every previously issued, still-unexpired token (TTL up to
   `write_token_expiry_minutes`, default 15) fails `parse_confirmation_token` with `TOKEN_INVALID`,
   despite being legitimately issued.
2. **During a rolling restart**, old-secret and new-secret replicas coexist; a token issued by one
   cannot be verified by the other — defeating the documented design goal that "any server replica
   sharing the same `client_secret` can verify a token issued by any other replica."

Identified by the deployment-aware codebase review as **P6 (High, 3/3 adversarial votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/token-signing-key-decouple` (off `master`)
- Files: `src/tools/_confirmation_token.py` (`_signing_key`, lines 58-59; used 99, 126),
  `src/tools/write.py` (passes `config.client_secret` at 213, 429, 464)
- Deployment: Azure Container Apps, multi-replica, secrets injected as env vars and rotated
- Deterministic at the tool-function level (no DB needed for the token path).

## Reproduction Steps

```python
import json
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.config import FabricSettings
from src.tools.write import register_write_tools

def tools(secret):
    cfg = FabricSettings(server="s.datawarehouse.fabric.microsoft.com", database="d",
        client_id="c", client_secret=secret, tenant_id="t",
        write_allowlist=["gold.transactions"], api_key="k")
    mcp = FastMCP("t"); db = MagicMock(); db.execute_write.return_value = 1
    register_write_tools(mcp, db, cfg)
    return {x.name: x.fn for x in mcp._tool_manager._tools.values()}

A = tools("SECRET-v1")
token = json.loads(A["fabric_preview_write"]("INSERT INTO gold.transactions (id) VALUES (1)"))["confirmation_token"]

B = tools("SECRET-v2")   # after AAD secret rotation / a peer replica with the new secret
print(json.loads(B["fabric_execute_write"](token)))
```

### Observed output (2026-06-03)

```
token issued under client_secret=SECRET-v1
redeem under client_secret=SECRET-v2 -> TOKEN_INVALID
```

A legitimately-issued, unexpired token is rejected purely because the AAD `client_secret` rotated
(or because a different replica holds the new secret).

## Expected vs Actual Behavior

| Scenario | Expected | Actual |
|---|---|---|
| Redeem an in-flight token after `client_secret` rotates (dedicated signing key unchanged) | succeeds | `TOKEN_INVALID` |
| Redeem a token on a peer replica during rolling restart (shared signing key) | succeeds | `TOKEN_INVALID` if the peer already has the new secret |
| Redeem with a genuinely wrong/forged signature | `TOKEN_INVALID` | `TOKEN_INVALID` ✓ (must stay) |

**Why:** the signing key is a function of `client_secret`, so any change to `client_secret` changes the
key and invalidates outstanding tokens.

## Test feasibility

Stable failing unit tests are feasible at the write-tool level (preview under one config, redeem under a
config with a rotated `client_secret`). No DB needed.
