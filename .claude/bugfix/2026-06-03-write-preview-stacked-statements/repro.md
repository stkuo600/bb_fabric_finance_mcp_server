# Repro — Write allowlist bypass via stacked statements in fabric_preview_write

## Symptom

`fabric_preview_write` validates the write allowlist against **only the first table token** it
parses from the SQL, but signs the **entire original SQL** into the confirmation token.
`fabric_execute_write` (and `fabric_execute_write_batch`) then execute `payload.sql` **verbatim with
no re-validation**. Because pyodbc / SQL Server runs every `;`-separated statement in one batch under
the privileged service principal, a payload whose first statement targets an allowlisted table and
whose trailing statement targets a **non-allowlisted** table passes preview, mints a valid token, and
executes the trailing statement on redemption — defeating the write allowlist.

Identified by the deployment-aware codebase review as **P4 (High, 3/3 adversarial votes)**; same
root mechanism (`;`-stacked statements) as P3.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/write-preview-stacked-statements` (off `master`)
- File: `src/tools/write.py` — `_parse_write_sql` (lines 142-153), `fabric_preview_write` (388-446),
  `fabric_execute_write` (448-487)
- Allowlist check: `src/tools/_validators.py::validate_writable_table`
- Python 3.11+/3.12; backend Microsoft Fabric SQL warehouse via pyodbc; privileged service principal
- Deterministic; no network/DB needed — reproducible at the tool-function level with a mocked db

## Reproduction Steps

```python
import json
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.config import FabricSettings
from src.tools.write import register_write_tools
from src.tools._confirmation_token import parse_confirmation_token

cfg = FabricSettings(server="test.datawarehouse.fabric.microsoft.com", database="db",
    client_id="cid", client_secret="cs", tenant_id="tid",
    write_allowlist=["gold.transactions", "gold.accounts"], api_key="k")
mcp = FastMCP("t"); db = MagicMock(); register_write_tools(mcp, db, cfg)
tools = {t.name: t.fn for t in mcp._tool_manager._tools.values()}

stacked = "INSERT INTO gold.transactions (id) VALUES (1); UPDATE secret.audit_log SET role=99"
res = json.loads(tools["fabric_preview_write"](stacked))
print("got token?", "confirmation_token" in res, "| reported table:", res.get("table"))
payload = parse_confirmation_token(res["confirmation_token"], cfg.client_secret)
print("signed sql ->", payload.sql)
```

### Observed output (2026-06-03)

```
got token? True | reported table: gold.transactions
signed sql -> INSERT INTO gold.transactions (id) VALUES (1); UPDATE secret.audit_log SET role=99
```

The preview is **accepted** (a token is issued) although the SQL writes to `secret.audit_log`, which
is **not** on the allowlist. The trailing UPDATE is signed into the token and would execute verbatim
on `fabric_execute_write`.

## Expected vs Actual Behavior

| Input to `fabric_preview_write` | Expected | Actual |
|---|---|---|
| `INSERT INTO gold.transactions ...; UPDATE secret.audit_log ...` | rejected (`INVALID_OPERATION`) | token issued ✗ |
| `INSERT INTO gold.transactions ...; DROP TABLE x` | rejected (`INVALID_OPERATION`) | token issued ✗ |
| `INSERT INTO gold.transactions (id) VALUES (1)` (single stmt) | token issued | token issued ✓ |
| `UPDATE gold.accounts SET balance = 100` (single stmt) | token issued | token issued ✓ |

**Why it slips through:** `_parse_write_sql` uses `^\s*INSERT\s+INTO\s+(\S+)` / `^\s*UPDATE\s+(\S+)`,
capturing only the first statement's table. `validate_writable_table` checks that one token; the SQL is
never constrained to a single statement, and redemption re-executes the full signed SQL without
re-validating.

## Test feasibility

A stable failing unit test is fully feasible at the `fabric_preview_write` tool level (mocked db, no
network). No fallback to manual reproduction needed.
