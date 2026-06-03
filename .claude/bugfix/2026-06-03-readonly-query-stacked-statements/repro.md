# Repro — Read-only query guard allows stacked DDL/DCL/EXEC statements

## Symptom

`fabric_execute_query` advertises itself as **read-only** (accepts only `SELECT` / `WITH ... SELECT`).
Its guard `src/tools/query.py::_is_read_only_query` accepts SQL that begins with `SELECT`/`WITH`
but then carries one or more additional `;`-separated statements containing **arbitrary DDL/DCL/EXEC**
(`DROP`, `TRUNCATE`, `ALTER`, `CREATE`, `GRANT`/`REVOKE`, `EXEC`/`EXECUTE`, `CALL`).

Because pyodbc / SQL Server executes all `;`-separated statements in a single batch, and the Fabric
connection authenticates with a **privileged client-credentials service principal**, an authenticated
MCP caller can run arbitrary destructive DDL/DCL against the entire warehouse through the "read-only"
tool — completely bypassing the write allowlist and two-phase confirmation-token flow.

Identified by the deployment-aware codebase review as **P3 (Critical, 3/3 adversarial votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `master`
- File: `src/tools/query.py` (`_is_read_only_query`, lines 38-49)
- Python 3.11+/3.12; backend Microsoft Fabric SQL warehouse via pyodbc/ODBC Driver 18
- Deployment: Azure Container Apps; Fabric connection uses a privileged service principal
- Deterministic; no network, DB, or timing dependency — reproducible at the pure-function level

## Reproduction Steps

Run against the actual guard function:

```python
from src.tools.query import _is_read_only_query as g
g("SELECT 1; DROP TABLE raw.Fact_Sch1X")            # -> True  (should be False)
g("SELECT 1; EXEC sp_who")                          # -> True  (should be False)
g("SELECT 1; TRUNCATE TABLE raw.Fact_ExchangeRate") # -> True  (should be False)
g("SELECT * FROM x; ALTER TABLE x ADD c INT")       # -> True  (should be False)
g("SELECT 1; GRANT CONTROL ON DATABASE::wh TO atk") # -> True  (should be False)
g("SELECT 1; CREATE TABLE foo(a int)")              # -> True  (should be False)
g("SELECT col FROM raw.Dim_Entity")                 # -> True  (legitimately allowed)
```

### Observed output (2026-06-03)

```
True <-- SELECT 1; DROP TABLE raw.Fact_Sch1X
True <-- SELECT 1; EXEC sp_who
True <-- SELECT 1; TRUNCATE TABLE raw.Fact_ExchangeRate
True <-- SELECT * FROM x; ALTER TABLE x ADD c INT
True <-- SELECT 1; GRANT CONTROL ON DATABASE::wh TO attacker
True <-- SELECT 1; CREATE TABLE foo(a int)
True <-- SELECT col FROM raw.Dim_Entity
```

## Expected vs Actual Behavior

| Input | Expected | Actual |
|-------|----------|--------|
| `SELECT 1; DROP TABLE ...` | rejected (`False`) | accepted (`True`) |
| `SELECT 1; EXEC sp_who` | rejected (`False`) | accepted (`True`) |
| `SELECT 1; TRUNCATE TABLE ...` | rejected (`False`) | accepted (`True`) |
| `SELECT ...; ALTER TABLE ...` | rejected (`False`) | accepted (`True`) |
| `SELECT 1; GRANT ...` | rejected (`False`) | accepted (`True`) |
| `SELECT 1; CREATE TABLE ...` | rejected (`False`) | accepted (`True`) |
| single `SELECT ...` | accepted (`True`) | accepted (`True`) ✓ |

**Why it slips through:** `_is_read_only_query` only (1) checks the string starts with `SELECT`/`WITH`
and (2) rejects if `INSERT|UPDATE|DELETE|MERGE` survives literal/comment stripping. It never rejects on
the `;` statement separator, and its keyword blocklist omits all DDL/DCL/EXEC verbs. A trailing
stacked statement therefore passes both checks.

## Test feasibility

A stable failing unit test is fully feasible at the `_is_read_only_query` level (pure function, no DB).
No need to fall back to manual reproduction.
