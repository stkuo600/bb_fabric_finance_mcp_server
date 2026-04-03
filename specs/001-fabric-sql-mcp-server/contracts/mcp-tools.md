# MCP Tool Contracts: Fabric SQL MCP Server

**Date**: 2026-04-02 | **Branch**: `001-fabric-sql-mcp-server`

## Tool: `fabric_execute_query`

Execute a read-only SQL query against the Fabric data warehouse.

**Parameters**:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| sql | string | yes | SQL SELECT statement to execute |

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
- Only SELECT statements are accepted. Non-SELECT statements return error code `INVALID_OPERATION`.
- Results limited to `max_rows` (default 500). If truncated, `truncated: true` is set.
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
- Token expires after 5 minutes.
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
  "columns": [
    {"name": "id", "type": "int", "nullable": false},
    {"name": "amount", "type": "decimal(18,2)", "nullable": false},
    {"name": "description", "type": "nvarchar(255)", "nullable": true},
    {"name": "created_at", "type": "datetime2", "nullable": false}
  ]
}
```

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
| TOKEN_EXPIRED | Write confirmation token has expired |
| TOKEN_INVALID | Write confirmation token not found or already used |
| CONFIG_ERROR | Server misconfiguration |
