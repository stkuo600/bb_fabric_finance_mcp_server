# Repro — fabric_execute_query fetches the whole result set before truncating

## Symptom

`fabric_execute_query` applies `effective_cap` (`max_rows`) in Python **after**
`db.execute_query` has already drained the entire cursor into a list of dicts. `execute_query` loops
`fetchmany` until exhausted with no row limit pushed to SQL or to the fetch loop. A
`SELECT * FROM big_fact` returning millions of rows materialises millions of Python dicts before all but
the default 500 are discarded — which can OOM-kill the (memory-limited, autoscaled) Azure Container Apps
replica, dropping every concurrent in-flight request on it.

Identified by the deployment-aware codebase review as **P8 (Medium, 3/3 votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/query-bounded-fetch` (off `master`)
- Files: `src/database.py` `execute_query` (drains cursor, lines ~300-330);
  `src/tools/query.py` (`effective_cap` applied after fetch, lines ~84-91)
- Deployment: Azure Container Apps, memory-limited replicas
- Deterministic at the `FabricDatabase` level with a mocked pyodbc cursor.

## Reproduction Steps

```python
from unittest.mock import MagicMock, patch
from src.database import FabricDatabase

def make():
    a = MagicMock(); a.get_token.return_value = "t"
    return FabricDatabase(server="s.datawarehouse.fabric.microsoft.com", database="d", auth=a)

with patch("src.database.pyodbc.connect") as mc:
    cur = MagicMock(); cur.description = [("id", int, None, None, None, None, False)]
    cur.fetchmany.side_effect = [[(i,) for i in range(1_000_000)], []]
    mc.return_value.cursor.return_value = cur
    _, rows = make().execute_query("SELECT * FROM huge", timeout=30)
    print("rows materialized:", len(rows))
```

### Observed output (2026-06-03)

```
rows materialized by execute_query: 1000000
```

The whole result set is materialised regardless of `max_rows`; the tool truncates only afterward.

## Expected vs Actual Behavior

| Scenario | Expected | Actual |
|---|---|---|
| `SELECT` returning millions, `max_rows=500` | fetch at most ~501 rows (cap + 1 to detect truncation) | all millions materialised |
| Result within the cap | all rows returned, `truncated=false` | correct ✓ |
| Truncation flag when over cap | `truncated=true` | correct ✓ (but only after draining) |

**Why:** `execute_query` has no row bound; `fabric_execute_query` truncates the returned list after the
full fetch.

## Test feasibility

Stable failing unit test at the `FabricDatabase` level (mock a large first chunk, assert the fetch stops
at `cap + 1` and does not drain). Plus a tool-level test that the cap is passed to `execute_query`.
