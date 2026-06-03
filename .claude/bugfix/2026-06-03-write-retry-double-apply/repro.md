# Repro — Non-idempotent write retry double-applies writes (P1 / P2 / P10)

## Symptom

Under `autocommit=True`, `FabricDatabase` treats **all** connection-class pyodbc errors
(`08*`, `IMC*`, and crucially `HYT*` timeouts) as "the statement never ran" and re-executes the
**same** write once on a fresh connection. But a connection drop (`08S01`) or client timeout (`HYT00`)
that fires **after** the Fabric server has already committed (but before the driver reads the ack)
is ambiguous — re-executing double-applies a non-idempotent write.

Three linked defects (one work-stream):

- **P1 (Critical):** `execute_write` → `_execute_with_retry` retries single writes on connection
  errors → duplicate INSERT / double-applied UPDATE.
- **P2 (Critical):** `execute_writes` (batch) re-executes the failed statement on a connection drop →
  duplicate row, reported as `status=ok` so the duplicate is invisible.
- **P10 (Low, amplifier):** `execute_query` sets `conn.timeout = 30` on the shared cached connection
  and never resets it; subsequent writes inherit the residual 30s. A long write then aborts with
  `HYT00`, which `_is_connection_error` classifies as a connection error → feeds the P1/P2 retry path.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/write-retry-unknown-state` (off `master`)
- File: `src/database.py` — `_execute_with_retry` (210-298, retry at 252-264), `execute_write`
  (332-343), `execute_writes` (345-438, retry at 383-398), `execute_query` `conn.timeout` (309)
- `autocommit=True` at `_open_connection` (192); `_is_connection_error` treats `HYT*` as connection
  error (75, 110-114)
- Python 3.11+/3.12; Microsoft Fabric SQL over pyodbc; deployed on Azure Container Apps over an
  explicitly unreliable Fabric network (mid-query drops are documented:
  `.claude/bugfix/2026-05-23-fabric-08s01-mid-query-tcp-drop`)
- Deterministic at the unit level with a mocked pyodbc connection.

## Reproduction Steps

```python
from unittest.mock import MagicMock, patch
import pyodbc
from src.database import FabricDatabase

def make():
    a = MagicMock(); a.get_token.return_value = "t"
    return FabricDatabase(server="s.datawarehouse.fabric.microsoft.com", database="d", auth=a)

# P1: a write that drops mid-flight (08S01) is RE-EXECUTED
with patch("src.database.pyodbc.connect") as mc:
    cur = MagicMock(); cur.rowcount = 1
    cur.execute.side_effect = [pyodbc.Error("08S01", "Communication link failure"), None]
    mc.return_value.cursor.return_value = cur
    make().execute_write("INSERT INTO raw.Fact_Sch1X (x) VALUES (1)")
    print("execute calls:", cur.execute.call_count)   # 2 -> double INSERT

# P10: write inherits a prior query's 30s timeout
with patch("src.database.pyodbc.connect") as mc:
    conn = mc.return_value
    cur = MagicMock(); cur.description = [("id", int, None, None, None, None, False)]
    cur.fetchmany.return_value = []; cur.rowcount = 1; conn.cursor.return_value = cur
    db = make(); db.execute_query("SELECT 1", timeout=30); db.execute_write("UPDATE t SET c=1")
    print("write timeout:", conn.timeout)             # 30 -> leaked
```

### Observed output (2026-06-03)

```
P1: cursor.execute called 2x  -> statement RE-EXECUTED (double INSERT), returned rows=1
P2: results=[1, 1, 1]         -> stmt b RE-EXECUTED on reconnect (badc 2x + goodc 2x)
P10: after query conn.timeout=30; after write conn.timeout=30  -> leaked to the write
```

## Expected vs Actual Behavior

| Scenario | Expected | Actual |
|---|---|---|
| Single write drops mid-flight (`08S01`/`HYT00`) | NOT re-executed; surfaced as unknown-commit-state so the caller can reconcile | re-executed once → duplicate write |
| Batch statement drops mid-flight | failed slot flagged unknown-state; later statements continue | failed statement re-executed → duplicate row reported as `ok` |
| Write after a query | write timeout is deterministic, independent of the prior query | write inherits the query's residual `conn.timeout` |
| Read (SELECT) drops mid-flight | reconnect-and-retry (idempotent — safe) | reconnect-and-retry ✓ (must stay) |

## Test feasibility

Stable failing unit tests are feasible at the `FabricDatabase` level with a mocked pyodbc connection
(existing `tests/unit/test_database.py` already mocks `pyodbc.connect`). The existing
`test_execute_writes_connection_error_mid_batch_reconnects` and
`test_execute_writes_connection_error_twice_records_error` assert the **unsafe** retry behavior and
must be updated to the safe (no-retry-for-writes) contract.
