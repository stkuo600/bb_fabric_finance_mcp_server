# Repro — fabric_execute_write docstring claims 5-minute token validity (actual: configurable, default 15)

## Symptom

`fabric_execute_write`'s docstring states the token "must not be expired (5-minute validity)", but the
actual expiry comes from `config.write_token_expiry_minutes` (default **15**, range 1–60, env
`FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`), applied in `fabric_preview_write` via
`timedelta(minutes=config.write_token_expiry_minutes)`. The `@mcp.tool()` docstring is the contract the
LLM reads to decide retry/timing behavior, so the wrong figure can cause premature re-preview or a wrong
explanation of expiry to users. No runtime impact (`ensure_token_not_expired` uses the real embedded
expiry).

Identified by the deployment-aware codebase review as **P11 + P13 (Low, same docstring, same root
cause — fix together)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/write-docstring-token-expiry` (off `master`)
- File: `src/tools/write.py` — `fabric_execute_write` docstring (line ~453); real expiry at line ~430
- Deterministic; inspect the registered tool's docstring.

## Reproduction Steps

```python
import inspect
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.config import FabricSettings
from src.tools.write import register_write_tools

cfg = FabricSettings(server="s.datawarehouse.fabric.microsoft.com", database="d", client_id="c",
    client_secret="cs", tenant_id="t", write_allowlist=["gold.t"], api_key="k")
mcp = FastMCP("t"); register_write_tools(mcp, MagicMock(), cfg)
fn = {t.name: t.fn for t in mcp._tool_manager._tools.values()}["fabric_execute_write"]
print("5-minute" in (fn.__doc__ or ""))    # True -> stale claim
print(cfg.write_token_expiry_minutes)        # 15 -> actual default
```

### Observed (2026-06-03)

```
docstring contains "5-minute": True
actual default write_token_expiry_minutes: 15
```

## Expected vs Actual Behavior

| Aspect | Expected | Actual |
|---|---|---|
| Docstring expiry claim | references the configurable expiry (default 15, `FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`) | hard-coded "5-minute validity" |
| Runtime enforcement | uses embedded `exp` | correct ✓ (unaffected) |

## Test feasibility

Stable test asserting the registered tool's docstring no longer says "5-minute" and references the
configurable expiry.
