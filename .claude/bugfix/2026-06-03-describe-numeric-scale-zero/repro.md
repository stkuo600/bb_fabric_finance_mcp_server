# Repro — fabric_describe_table drops precision for zero-scale numeric types

## Symptom

In `fabric_describe_table`, the numeric formatting branch is
`elif row.get("NUMERIC_PRECISION") and row.get("NUMERIC_SCALE"):` — it requires **both** to be truthy.
For SQL Server, `NUMERIC_SCALE` is `0` for integer-like numerics (`DECIMAL(18,0)`, `NUMERIC(38,0)`),
and `0` is falsy in Python, so the branch is skipped and the column type is reported as bare `decimal`
/ `numeric`, losing the declared precision. Misleads the LLM/caller about the column type.

Identified by the deployment-aware codebase review as **P7 (Medium, 2/3 votes — confirmed as a logic
defect)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/describe-table-numeric-scale-zero` (off `master`)
- File: `src/tools/schema.py` — `fabric_describe_table` numeric branch (line ~141)
- Deterministic at the tool-function level with a mocked `db.execute_query`.

## Reproduction Steps

```python
import json
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.tools.schema import register_schema_tools
from src.models import ColumnInfo

db = MagicMock()
db.execute_query.return_value = ([ColumnInfo(name="x", type="str", nullable=False)], [
    {"TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE", "COLUMN_NAME": "amount",
     "DATA_TYPE": "decimal", "IS_NULLABLE": "NO", "CHARACTER_MAXIMUM_LENGTH": None,
     "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 0},
])
mcp = FastMCP("t"); register_schema_tools(mcp, db)
fn = {t.name: t.fn for t in mcp._tool_manager._tools.values()}["fabric_describe_table"]
print(json.loads(fn("gold.t"))["columns"][0]["type"])
```

### Observed output (2026-06-03)

```
amount -> decimal          # DECIMAL(18,0): precision lost
rate   -> decimal(18,6)    # DECIMAL(18,6): correct
```

## Expected vs Actual Behavior

| Column | Expected | Actual |
|---|---|---|
| `DECIMAL(18,0)` | `decimal(18,0)` | `decimal` |
| `DECIMAL(18,6)` | `decimal(18,6)` | `decimal(18,6)` ✓ |
| `int` (no precision) | `int` | `int` ✓ |

**Why:** `row.get("NUMERIC_SCALE")` is `0` → falsy → the precision/scale branch is skipped.

## Test feasibility

Stable failing unit test at the tool-function level with a mocked `db.execute_query`.
