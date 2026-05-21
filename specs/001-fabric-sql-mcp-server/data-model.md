# Data Model: Fabric SQL MCP Server

**Date**: 2026-04-02 | **Branch**: `001-fabric-sql-mcp-server`

## Internal Entities (Pydantic models in the server)

### FabricConfig

Represents the server's connection and behavior configuration.

| Field | Type | Required | Source | Description |
|-------|------|----------|--------|-------------|
| server | string | yes | `FABRIC_SERVER` env / config file | Fabric SQL endpoint hostname |
| database | string | yes | `FABRIC_DATABASE` env / config file | Warehouse database name |
| client_id | string | yes | `FABRIC_CLIENT_ID` env / config file | Azure AD app client ID |
| client_secret | string | yes | `FABRIC_CLIENT_SECRET` env / config file | Azure AD app client secret |
| tenant_id | string | yes | `FABRIC_TENANT_ID` env / config file | Azure AD tenant ID |
| write_allowlist | list[string] | no | `FABRIC_WRITE_ALLOWLIST` env / config file | Tables permitted for write operations (empty = no writes allowed) |
| max_rows | integer | no | `FABRIC_MAX_ROWS` env / config file | Max rows returned per query (default: 500) |
| port | integer | no | `FABRIC_PORT` env / config file | HTTP server port (default: 8000) |

**Validation rules**:
- `server` must end with `.datawarehouse.fabric.microsoft.com`
- `max_rows` must be between 1 and 10000
- `write_allowlist` items must be non-empty strings in `schema.table` or `table` format

### QueryResult

Represents the output of a SELECT query.

| Field | Type | Description |
|-------|------|-------------|
| columns | list[ColumnInfo] | Column metadata |
| rows | list[dict[string, any]] | Result data as JSON objects |
| row_count | integer | Number of rows returned |
| truncated | boolean | True if results were limited by max_rows |

### ColumnInfo

| Field | Type | Description |
|-------|------|-------------|
| name | string | Column name |
| type | string | SQL data type |
| nullable | boolean | Whether column allows NULL |

### WriteResult

Represents the output of an INSERT/UPDATE operation.

| Field | Type | Description |
|-------|------|-------------|
| affected_rows | integer | Number of rows affected |
| operation | string | "INSERT" or "UPDATE" |
| table | string | Target table name |

### WritePreview

Represents the preview returned before write confirmation.

| Field | Type | Description |
|-------|------|-------------|
| confirmation_token | string | HMAC-signed self-contained token (`<payload_b64url>.<sig_b64url>`) for confirming execution. Stateless — verifiable on any replica sharing the same `client_secret`. |
| operation | string | "INSERT" or "UPDATE" |
| table | string | Target table name |
| sql_summary | string | Human-readable summary of the operation |
| expires_at | string | ISO 8601 timestamp when token expires |

### TableInfo

| Field | Type | Description |
|-------|------|-------------|
| schema_name | string | Schema (e.g., "dbo") |
| table_name | string | Table name |
| table_type | string | "BASE TABLE" or "VIEW" |

### ErrorResponse

Uniform error structure per Constitution Principle III.

| Field | Type | Description |
|-------|------|-------------|
| code | string | Error code (e.g., "AUTH_FAILED", "QUERY_ERROR", "TABLE_NOT_ALLOWED") |
| message | string | Human-readable error description |
| details | string or null | Additional context (optional) |

## External Data (Fabric Warehouse)

The server does not own or manage the warehouse schema. It interacts with whatever tables exist in the configured database. Schema discovery queries `INFORMATION_SCHEMA.TABLES` and `INFORMATION_SCHEMA.COLUMNS`.

## State Management

- **Token cache**: In-memory MSAL token cache (single access token, auto-refreshed).
- **Confirmation tokens**: Stateless HMAC-SHA256 signed tokens. The token itself carries the SQL, operation, target table, expiry (POSIX seconds), and a random nonce; the signing key is derived from `config.client_secret`. No server-side store. Verifiable on any replica that shares the same `client_secret`. Tokens expire 5 minutes after issue. **Replayable within that window** — see `.claude/bugfix/2026-05-22-write-token-cross-instance/report.md` for the cross-replica vs single-use trade-off.
- **No persistent state**: The server is stateless between restarts. No local database or file storage.
