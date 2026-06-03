# MCP Tool Contracts: Fabric SQL MCP Server

**Date**: 2026-04-02 | **Branch**: `001-fabric-sql-mcp-server`

## Tool: `fabric_execute_query`

Execute a read-only SQL query against the Fabric data warehouse.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| sql | string | yes | Read-only SQL: a `SELECT`, or a CTE-prefixed `WITH ... SELECT` |
| max_rows | integer | no | Per-call row cap (1–10000). Overrides the server default. |

**Returns** (success):
```json
{
  "columns": [
    {"name": "id", "type": "int", "nullable": false},
    {"name": "name", "type": "nvarchar", "nullable": true}
  ],
  "rows": [
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"}
  ],
  "row_count": 2,
  "truncated": false
}
```

**Returns** (error):
```json
{
  "code": "QUERY_ERROR",
  "message": "Invalid SQL syntax near 'SELCT'",
  "details": "Line 1, Column 1"
}
```

**Behavior**:
- Only read-only queries are accepted: a `SELECT`, or a CTE-prefixed `WITH ... SELECT`. CTE-prefixed write DML (`WITH ... INSERT/UPDATE/DELETE/MERGE`) and any other write statements return error code `INVALID_OPERATION`. Writes must go through `fabric_preview_write` / `fabric_execute_write`.
- Results limited to `max_rows` (call-site override, else server-config default of 500). If truncated, `truncated: true` is set. Per-call `max_rows` outside `[1, 10000]` returns `INVALID_OPERATION`.
- Query timeout: 30 seconds.

---

## Tool: `fabric_preview_write`

Preview a write operation and receive a confirmation token. Does NOT execute the SQL.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| sql | string | yes | SQL INSERT or UPDATE statement |

**Returns** (success):
```json
{
  "confirmation_token": "550e8400-e29b-41d4-a716-446655440000",
  "operation": "INSERT",
  "table": "dbo.transactions",
  "sql_summary": "INSERT into dbo.transactions: 1 row with columns [id, amount, date]",
  "expires_at": "2026-04-02T12:05:00Z"
}
```

**Returns** (error - table not on allowlist):
```json
{
  "code": "TABLE_NOT_ALLOWED",
  "message": "Table 'dbo.audit_log' is not on the write allowlist",
  "details": "Allowed tables: dbo.transactions, dbo.accounts"
}
```

**Behavior**:
- Only INSERT and UPDATE statements are accepted.
- Target table must be on the configured write allowlist.
- Token expires after `write_token_expiry_minutes` (default 15, configurable 1–60 via `FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`).
- Token payload carries `iat` (issued-at, POSIX seconds) alongside `exp` for forensic / audit use; `iat` is informational only and is not enforced by the verifier.
- Does NOT execute the SQL — only validates and returns a preview.

---

## Tool: `fabric_execute_write`

Execute a previously previewed write operation using a confirmation token.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| confirmation_token | string | yes | Token from `fabric_preview_write` |

**Returns** (success):
```json
{
  "affected_rows": 1,
  "operation": "INSERT",
  "table": "dbo.transactions"
}
```

**Returns** (error - expired token):
```json
{
  "code": "TOKEN_EXPIRED",
  "message": "Confirmation token has expired. Please preview the write operation again.",
  "details": null
}
```

**Behavior**:
- Token must exist and not be expired.
- Each token can only be used once.
- After execution (success or failure), the token is invalidated.

---

## Tool: `fabric_execute_write_batch`

Redeem multiple confirmation tokens in a single MCP round-trip. Intended for batch INSERT/UPDATE workflows where calling `fabric_execute_write` once per row would mean 2N tool calls for N rows.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| confirmation_tokens | array of string | yes | List of tokens previously issued by `fabric_preview_write`. Max 100 per call. |

**Returns** (success — note that "success" here means the request was well-formed; individual tokens may still have failed):
```json
{
  "results": [
    {"status": "ok", "affected_rows": 1, "operation": "INSERT", "table": "raw.Fact_X"},
    {"status": "error", "code": "TOKEN_EXPIRED", "message": "...", "operation": "INSERT", "table": "raw.Fact_X"},
    {"status": "ok", "affected_rows": 1, "operation": "INSERT", "table": "raw.Fact_X"}
  ],
  "total_succeeded": 2,
  "total_failed": 1
}
```

**Returns** (error — batch rejected outright):
```json
{
  "code": "INVALID_OPERATION",
  "message": "Batch size 142 exceeds the limit of 100 tokens.",
  "details": null
}
```

**Behavior**:
- **Best-effort**: each token is verified and executed independently. A failure on one token (`TOKEN_INVALID`, `TOKEN_EXPIRED`, or a `QUERY_ERROR` from the database) does not roll back earlier successes nor prevent later tokens from running.
- Empty list is a successful no-op: `{"results": [], "total_succeeded": 0, "total_failed": 0}`.
- More than 100 tokens returns `INVALID_OPERATION` without verifying any of them.
- Each successful execution emits an audit log line (same shape as `fabric_execute_write`), plus a batch summary at completion.
- **Atomicity is not provided.** If the workflow requires "all rows commit or none", callers should pre-validate (e.g. `fabric_execute_query("SELECT COUNT(*) ...")`) and accept the residual risk, or stick with `fabric_execute_write` and implement compensating actions in the caller.

---

## Tool: `fabric_list_writable_tables`

List tables on the write allowlist — i.e. those that may be the target of `fabric_preview_write` / `fabric_execute_write` / `fabric_delete_period`.

**Parameters**: none.

**Returns** (success):
```json
{
  "writable_tables": ["raw.Fact_ExchangeRate", "gold.Dim_Entity"]
}
```

**Behavior**:
- Pure read of server configuration (`write_allowlist` / `FABRIC_WRITE_ALLOWLIST`). No database round-trip.
- Returns an empty list when no tables are configured.

---

## Tool: `fabric_delete_period`

Delete one fiscal period's rows from an allowlisted fact table. The WHERE clause is fixed — arbitrary DELETE is not supported. Intended for monthly fact-table reload workflows (e.g. FX rate re-import).

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| table | string | yes | Schema-qualified target (e.g. `"raw.Fact_ExchangeRate"`). Must be on the write allowlist **and** have both `FiscalYear` and `FiscalMonth` columns. |
| fiscal_year | integer | yes | Four-digit fiscal year (1900–9999). |
| fiscal_month | integer | yes | Fiscal month (1–12). |

**Returns** (success):
```json
{
  "deleted_rows": 143,
  "table": "raw.Fact_ExchangeRate",
  "fiscal_year": 2026,
  "fiscal_month": 5
}
```

**Returns** (error):
- `TABLE_NOT_ALLOWED` — target not on the write allowlist.
- `INVALID_OPERATION` — unqualified table name, year/month out of range, or required `FiscalYear`/`FiscalMonth` column missing on the target.

**Behavior**:
- Issues `DELETE FROM <table> WHERE FiscalYear = <fy> AND FiscalMonth = <fm>` — no other WHERE conditions are supported.
- Pre-flight column-existence check against `INFORMATION_SCHEMA.COLUMNS` so dimension tables (which lack fiscal columns) are refused with a clear message rather than a cryptic SQL error.
- Zero rows deleted is a successful no-op (`{"deleted_rows": 0, ...}`), not an error.
- Audited via structured log entry containing tool name, table, fiscal_year, fiscal_month, and row count.

---

## Tool: `fabric_list_schemas`

List all database schemas in the connected data warehouse.

**Parameters**: None

**Returns** (success):
```json
{
  "schemas": [
    {"schema_name": "raw"},
    {"schema_name": "gold"}
  ]
}
```

**Behavior**:
- Queries `INFORMATION_SCHEMA.SCHEMATA` (excluding system schemas like `sys`, `INFORMATION_SCHEMA`).
- Useful for LLMs to understand warehouse structure before querying tables.

---

## Tool: `fabric_list_tables`

List all tables and views in the connected data warehouse. Optionally filter by schema.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| schema_name | string | no | Filter tables by schema (e.g., "gold"). If omitted, returns all schemas. |

**Returns** (success):
```json
{
  "tables": [
    {"schema_name": "gold", "table_name": "transactions", "table_type": "BASE TABLE"},
    {"schema_name": "gold", "table_name": "accounts", "table_type": "BASE TABLE"},
    {"schema_name": "raw", "table_name": "imports", "table_type": "BASE TABLE"}
  ]
}
```

---

## Tool: `fabric_describe_table`

Get column details for a specific table.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| table_name | string | yes | Table name (optionally schema-qualified, e.g., "gold.transactions") |

**Returns** (success):
```json
{
  "schema_name": "gold",
  "table_name": "transactions",
  "object_type": "BASE TABLE",
  "columns": [
    {"name": "id", "type": "int", "nullable": false},
    {"name": "amount", "type": "decimal(18,2)", "nullable": false},
    {"name": "description", "type": "nvarchar(255)", "nullable": true},
    {"name": "created_at", "type": "datetime2", "nullable": false}
  ]
}
```

`object_type` is `"BASE TABLE"` for tables and `"VIEW"` for views.

**Returns** (error - table not found):
```json
{
  "code": "TABLE_NOT_FOUND",
  "message": "Table 'gold.nonexistent' not found in the data warehouse",
  "details": null
}
```

---

## Error Code Reference

| Code | Description |
|------|-------------|
| AUTH_FAILED | Authentication with Fabric failed (invalid credentials or expired) |
| CONNECTION_ERROR | Cannot connect to Fabric SQL endpoint |
| QUERY_ERROR | SQL syntax or execution error |
| INVALID_OPERATION | Wrong SQL statement type for this tool |
| TABLE_NOT_ALLOWED | Target table not on write allowlist |
| TABLE_NOT_FOUND | Specified table does not exist |
| TABLE_AMBIGUOUS | An unqualified table name in `fabric_describe_table` exists in more than one schema; the message lists the candidate schemas. Re-run with a schema-qualified name. |
| TOKEN_EXPIRED | Write confirmation token has expired |
| TOKEN_INVALID | Write confirmation token is missing, malformed, or has an invalid signature |
| WRITE_STATE_UNKNOWN | Connection dropped mid-write; the statement may or may not have committed. Not auto-retried (would risk duplicate application under autocommit). Verify the table state before retrying. In `fabric_execute_write_batch`, the affected slot carries this code while other statements report their own status. |
| CONFIG_ERROR | Server misconfiguration |

### `QUERY_ERROR` details: Fabric-specific remediation hints

When a `QUERY_ERROR` response is generated, the `details` field carries a remediation hint when the underlying Fabric/SQL Server message matches a known limitation. The raw error stays in `message`. Known patterns:

- **IDENTITY overflow** (`message` contains "IDENTITY" + "overflow"/"arithmetic"): "Fabric Warehouse does not support widening an existing IDENTITY column via ALTER. Recreate the table with BIGINT IDENTITY and reload the data."
- **Unsupported `ALTER TABLE` DDL** (`message` contains an ALTER TABLE ADD/ALTER COLUMN reference flagged as unsupported): "Fabric Warehouse does not support ALTER TABLE ADD/ALTER COLUMN. DROP the table and recreate it with the desired schema, then reload the data."
- **No match**: `details` is `null` (no hint injected).
