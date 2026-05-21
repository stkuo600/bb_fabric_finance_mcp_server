## Symptom

User reports: `fabric_execute_query` rejects any SQL that begins with
`WITH` (Common Table Expression / CTE prefix), even when the eventual
operation is a pure SELECT. The tool returns `INVALID_OPERATION` with
the message "Only SELECT statements are allowed."

CTEs are a standard, read-only construct in T-SQL / Fabric Warehouse
analytics. The block forces users to rewrite as nested subqueries —
unnecessary friction for the common case.

## Environment

- Tool: `fabric_execute_query` in `src/tools/query.py`.
- Validation: `_SELECT_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)`
  on `src/tools/query.py:17`, checked at line 35.
- Spec: `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md:41`
  says "Only SELECT statements are accepted." The spec is the source
  of the over-strict rule; the regex is faithful to the spec.

## Reproduction Steps

Deterministic, no live Fabric required:

```python
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.tools.query import register_query_tools
from src.config import FabricSettings

cfg = FabricSettings(
    server="x.datawarehouse.fabric.microsoft.com", database="d",
    client_id="c", client_secret="s", tenant_id="t",
    api_key="k", max_rows=500,
)
mcp = FastMCP("test")
mock_db = MagicMock()
register_query_tools(mcp, mock_db, cfg)
fn = next(t for t in mcp._tool_manager._tools.values() if t.name == "fabric_execute_query").fn

import json
result = json.loads(fn("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte"))
print(result)  # → {'code': 'INVALID_OPERATION', ...}
```

## Expected vs Actual Behavior

**Expected:** A CTE-prefixed query whose ultimate operation is SELECT
must execute. E.g.

- `WITH cte AS (SELECT ...) SELECT * FROM cte`
- `WITH a AS (...), b AS (...) SELECT ... FROM a JOIN b ON ...`
- `WITH cte AS (SELECT ... UNION ALL SELECT ... FROM cte) SELECT * FROM cte` (recursive)

**Actual:** All of the above return `INVALID_OPERATION`.

A separate consideration: T-SQL also permits `WITH` to precede write
DML statements:

- `WITH cte AS (SELECT ...) INSERT INTO t SELECT * FROM cte`
- `WITH cte AS (SELECT ...) UPDATE t SET ...`
- `WITH cte AS (SELECT ...) DELETE FROM t ...`
- `WITH cte AS (SELECT ...) MERGE INTO t USING cte ...`

These must continue to be rejected — they are writes and must go
through the two-phase `fabric_preview_write` / `fabric_execute_write`
confirmation flow. Naively allowing any `WITH`-prefixed SQL would
open a write hole.
