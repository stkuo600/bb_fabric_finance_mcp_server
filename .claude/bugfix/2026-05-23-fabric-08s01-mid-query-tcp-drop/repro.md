## Symptom

Heavy read-only SELECT queries against the Fabric SQL endpoint fail with:

```
('08S01', '[08S01] [Microsoft][ODBC Driver 18 for SQL Server]
Communication link failure (0) (SQLExecDirectW)')
```

after **~43-45 seconds** end-to-end (measured by the MCP client).

Pattern:
- Small queries (e.g. `Dim_PLMapping`, `Fact_Sch1X`, `Dim_Entity`) succeed.
- The aggregating view `gold.vw_Sch1X_EntityUSD` for a single fiscal period
  across all entities consistently fails.
- Splitting by smaller entity groups *sometimes* succeeds — correlates with
  per-shard execution time staying under ~15s.

## Environment

- Production: Azure Container Apps, single replica, image
  `bb-fabric-finance-mcp-server:8dd11cb` (revision `--0000002`)
- Warehouse: `finance_data_warehouse` (Microsoft Fabric SQL endpoint)
- Driver: ODBC Driver 18 for SQL Server (Linux, msodbcsql18)
- Connection string (pre-fix):
  ```
  DRIVER={ODBC Driver 18 for SQL Server};
  SERVER=<…>.datawarehouse.fabric.microsoft.com,1433;
  DATABASE=finance_data_warehouse;
  Encrypt=yes;
  TrustServerCertificate=no;
  ConnectRetryCount=3;ConnectRetryInterval=10
  ```
  Notably **omits `KeepAlive` / `KeepAliveInterval`** → defaults to
  `KeepAlive=30s` (Microsoft Learn: *Connecting from Linux or macOS,
  Adjusting the TCP Keep-Alive Settings*).
- `Connection.timeout = 30` is applied in `execute_query` (per-query cap).
- Period queried: FiscalYear=2026, FiscalMonth=4.

## Reproduction Steps

### Production log evidence (deterministic timing)

`az containerapp logs show --type console` captured five consecutive failures
on `2026-05-23` in revision `--0000002`. Each query exhibits the same timing
to the millisecond:

| Query | Start (UTC) | App retry warn | Final error | T to retry | T to error |
|---|---|---|---|---|---|
| 2 | 13:26:30.762 | 13:26:51.415 | 13:27:11.785 | **20.65s** | **41.02s** |
| 3 | 13:27:16.405 | 13:27:36.938 | 13:27:57.485 | **20.53s** | **41.08s** |
| 4 | 13:28:18.711 | 13:28:39.156 | 13:28:59.870 | **20.44s** | **41.16s** |
| 5 | 13:29:50.356 | 13:30:10.832 | 13:30:31.391 | **20.48s** | **41.03s** |

Two stacked ~20s timeouts: first attempt → 20s → 08S01 → `_execute_with_retry`
discards + opens fresh connection + retries → 20s → 08S01 again →
`FabricQueryError` surfaced to caller. ~20s gap is **not** an artefact of our
`Connection.timeout=30` — the timeout would have produced `HYT00`/`HYT01`,
not `08S01`. The 20s is a *network-intermediary TCP idle drop*.

### Why the heavy view fails consistently while light queries don't

Heavy view (`gold.vw_Sch1X_EntityUSD`) takes ~18-22s of Fabric-side compute.
During that window the client→Fabric TCP connection is **silent** — no
packets flow. With `KeepAlive=30s` (driver default), the first keep-alive
probe is scheduled at T+30s — **after** the intermediary has already dropped
the silent connection at ~T+20s.

Light queries complete (and emit result packets) before the intermediary's
idle-drop threshold, so they succeed.

### Stable test-level reproduction

**Not feasible.** The failure is a network-layer interaction between:
- ODBC driver TCP keep-alive settings (client)
- Some Azure intermediary's TCP idle timeout (uncontrolled)
- Fabric Warehouse server-side query execution time (data-dependent)

None can be reliably mocked in a unit test. The deterministic part —
"connection string omits KeepAlive keywords" — is testable as a guard
(per Bug Fix Protocol's documented-reason fallback for env-dependent
failures).

## Expected vs Actual Behavior

| | Expected | Actual (current code) |
|---|---|---|
| Heavy view query (~20s Fabric-side) | TCP keep-alive packets sent every ≤10s keep the connection alive; query result returns; caller sees success after ~25s. | TCP goes silent for ~20s; intermediary drops the connection; driver raises 08S01; app retries once (same fate); caller sees 08S01 after ~40s. |
| Observability on failure | Log line carries `query_duration_ms`, `attempt`, `sql_hash`, `sqlstate` so an operator can correlate per-query timing without scraping timestamps. | Log line shows `tool` and `error_code` only; duration must be reconstructed from surrounding timestamps; SQL identity is implicit. |
| Fabric server-side telemetry | `queryinsights.exec_requests_history` can `WHERE program_name = 'fabric-mcp'` to filter our requests. | Default ApplicationName surfaces as generic ODBC driver string; our calls aren't distinguishable from other clients hitting the warehouse. |
