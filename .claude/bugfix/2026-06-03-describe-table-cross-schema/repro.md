# Repro — fabric_describe_table merges columns across same-named tables in different schemas

## Symptom

When `fabric_describe_table` is called with an **unqualified** table name (no `schema.` prefix) and
that name exists in more than one schema (e.g. `raw.transactions` and `gold.transactions`), the SQL
filters only on `c.TABLE_NAME` with no schema predicate. The query returns the **union** of all those
tables' columns, interleaved by `ORDINAL_POSITION`. `resolved_schema = rows[0]["TABLE_SCHEMA"]` reports
a single schema while the `columns` array silently contains columns from multiple physical tables, and
the column count is the sum across tables. No error is raised — the caller (typically an LLM) receives a
conflated, wrong table description.

Identified by the deployment-aware codebase review as **P5 (High, 3/3 adversarial votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/describe-table-cross-schema` (off `master`)
- File: `src/tools/schema.py` — `fabric_describe_table` (lines 94-163; defective SQL/merge at 105-156)
- Python 3.11+/3.12; Microsoft Fabric SQL warehouse
- Project uses a medallion `raw` / `gold` layout (memory: both schemas exist; `gold` currently being
  built out), so same-named tables across schemas is a realistic state.
- Deterministic at the tool-function level with a mocked `db.execute_query`.

## Reproduction Steps

```python
import json
from unittest.mock import MagicMock
from mcp.server.fastmcp import FastMCP
from src.tools.schema import register_schema_tools
from src.models import ColumnInfo

db = MagicMock()
# Unqualified 'transactions' exists in BOTH gold and raw; INFORMATION_SCHEMA
# returns the union interleaved by ORDINAL_POSITION.
db.execute_query.return_value = (
    [ColumnInfo(name="x", type="str", nullable=False)],
    [
        {"TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE", "COLUMN_NAME": "id",
         "DATA_TYPE": "int", "IS_NULLABLE": "NO", "CHARACTER_MAXIMUM_LENGTH": None,
         "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0},
        {"TABLE_SCHEMA": "raw", "TABLE_TYPE": "BASE TABLE", "COLUMN_NAME": "raw_blob",
         "DATA_TYPE": "varchar", "IS_NULLABLE": "YES", "CHARACTER_MAXIMUM_LENGTH": 4000,
         "NUMERIC_PRECISION": None, "NUMERIC_SCALE": None},
        {"TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE", "COLUMN_NAME": "amount",
         "DATA_TYPE": "decimal", "IS_NULLABLE": "YES", "CHARACTER_MAXIMUM_LENGTH": None,
         "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 2},
    ],
)
mcp = FastMCP("t"); register_schema_tools(mcp, db)
fn = {t.name: t.fn for t in mcp._tool_manager._tools.values()}["fabric_describe_table"]
print(json.loads(fn("transactions")))   # unqualified
```

### Observed output (2026-06-03)

```
resolved schema_name : gold
column count         : 3  (merged from gold+raw!)
columns              : ['id', 'raw_blob', 'amount']
```

`raw_blob` belongs to `raw.transactions`; `id`/`amount` belong to `gold.transactions`. They are merged
into one fictitious table description with `schema_name: "gold"`.

## Expected vs Actual Behavior

| Call | Expected | Actual |
|---|---|---|
| `describe("transactions")` where it exists in `gold` + `raw` | error identifying the ambiguity and listing candidate schemas, so the caller re-queries qualified | columns from both tables silently merged under one schema name |
| `describe("gold.transactions")` (qualified) | columns of exactly `gold.transactions` | correct ✓ (schema predicate applied) |
| `describe("transactions")` existing in exactly one schema | that table's columns | correct ✓ |

**Why it slips through:** with no `.` in the name, `schema = None`, so the `AND c.TABLE_SCHEMA = ...`
predicate is skipped. The result spans multiple schemas but the code takes `rows[0]["TABLE_SCHEMA"]`
as the single resolved schema and concatenates every row's column.

## Test feasibility

Stable failing unit tests are feasible at the tool-function level with a mocked `db.execute_query`
(matches existing `tests/unit/test_tools/test_schema.py` patterns). No DB needed.
