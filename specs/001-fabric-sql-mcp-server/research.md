# Research: Fabric SQL MCP Server

**Date**: 2026-04-02 | **Branch**: `001-fabric-sql-mcp-server`

## Decision 1: MCP Server Framework & Transport

**Decision**: Use `mcp` Python SDK with `FastMCP` class and `streamable-http` transport.

**Rationale**: The official MCP Python SDK (`mcp` package) provides `FastMCP` as the high-level server interface. Streamable-HTTP is the recommended production transport, supporting stateless JSON responses suitable for Azure Container Apps. The SDK handles tool registration, schema generation, and protocol compliance automatically via decorators and type hints.

**Alternatives considered**:
- stdio transport: Only works for local subprocess invocation, not suitable for HTTP deployment.
- SSE transport: Legacy, being superseded by streamable-http.
- Custom HTTP implementation: Unnecessary complexity; the SDK handles the protocol.

**Key details**:
- `FastMCP("ServerName", stateless_http=True, json_response=True)` for stateless HTTP mode.
- `mcp.run(transport="streamable-http")` serves on `/mcp` endpoint by default.
- Tools defined with `@mcp.tool()` decorator; type hints become JSON schema automatically.

## Decision 2: Database Driver & Connection

**Decision**: Use `pyodbc` with ODBC Driver 18 for SQL Server.

**Rationale**: pyodbc is the standard Python driver for Fabric SQL endpoints, which use the TDS protocol (same as Azure SQL/SQL Server). ODBC Driver 18 is required for Azure AD/Entra authentication and mandatory encryption.

**Alternatives considered**:
- `pymssql`: Does not support Azure AD authentication natively.
- `sqlalchemy` + `mssql+pyodbc`: Adds ORM overhead unnecessary for raw SQL execution. Could be added later if needed.
- `azure-identity` + `pyodbc`: Would work but adds another dependency when MSAL handles client credentials directly.

**Key details**:
- Connection string: `DRIVER={ODBC Driver 18 for SQL Server};SERVER=<server>,1433;DATABASE=<db>;Encrypt=yes;TrustServerCertificate=no`
- Token passed via `SQL_COPT_SS_ACCESS_TOKEN` attribute (1256) with UTF-16LE encoding and 4-byte length prefix.
- `autocommit=True` recommended (Fabric DW has limited transaction support).

## Decision 3: Authentication Flow

**Decision**: Use `msal` (Microsoft Authentication Library) with `ConfidentialClientApplication` for OAuth2 client credentials flow.

**Rationale**: MSAL is the official Microsoft library for Azure AD/Entra ID authentication. It handles token caching and automatic refresh. Client credentials flow is the correct pattern for service-to-service authentication without user interaction.

**Alternatives considered**:
- `azure-identity` `ClientSecretCredential`: Higher-level wrapper, but MSAL gives more control over token caching and is lighter-weight for this specific flow.
- Manual HTTP token requests: Unnecessary complexity; MSAL handles edge cases (token expiry, caching, retry).

**Key details**:
- Token endpoint: `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`
- Scope: `https://analysis.windows.net/powerbi/api/.default`
- MSAL caches tokens automatically; `acquire_token_silent()` then `acquire_token_for_client()` pattern.

## Decision 4: Write Operation Confirmation Pattern

**Decision**: Implement a two-phase write tool: first call returns a preview with a confirmation token, second call with the token executes the write.

**Rationale**: The MCP SDK's experimental `task.elicit()` API could handle confirmation, but it is marked experimental and may change. A simpler, stable approach is to implement two MCP tools: `fabric_preview_write` (returns SQL analysis + confirmation token) and `fabric_execute_write` (requires the token to proceed). This is more compatible across MCP clients.

**Alternatives considered**:
- MCP SDK experimental `task.elicit()`: Experimental API, may break. Not all clients support task mode.
- Single tool with `confirm=true` parameter: Less safe; LLM could always pass `true`.
- No confirmation: Rejected per spec (FR-011).

**Key details**:
- Preview tool parses SQL, identifies target table, checks allowlist, and returns affected table/operation summary.
- Confirmation token is a short-lived identifier (e.g., UUID) stored server-side.
- Execute tool validates token exists and hasn't expired before running the SQL.

## Decision 5: Configuration Loading

**Decision**: Use Pydantic `BaseSettings` for configuration with env var binding and optional JSON config file.

**Rationale**: Pydantic BaseSettings natively supports loading from environment variables and can be extended to read a JSON/YAML file. This gives type validation, clear error messages for missing fields, and env-var-takes-precedence behavior out of the box.

**Alternatives considered**:
- `python-dotenv` + manual parsing: More code, less type safety.
- `dynaconf`: Heavier dependency for a simple config structure.

**Key details**:
- Env var prefix: `FABRIC_` (e.g., `FABRIC_SERVER`, `FABRIC_DATABASE`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, `FABRIC_TENANT_ID`).
- Config file: `config.json` in project root (gitignored).
- Additional settings: `FABRIC_WRITE_ALLOWLIST` (comma-separated table names), `FABRIC_MAX_ROWS` (default 500).

## Decision 6: Schema Discovery Implementation

**Decision**: Use `INFORMATION_SCHEMA` views for table and column metadata.

**Rationale**: Standard T-SQL `INFORMATION_SCHEMA.TABLES` and `INFORMATION_SCHEMA.COLUMNS` views work on Fabric data warehouses and provide a stable, portable interface for metadata queries.

**Alternatives considered**:
- `sys.tables` / `sys.columns`: Faster for large schemas but less portable; can be optimized later if needed.

## Decision 7: Result Size Limiting

**Decision**: Default row limit of 500 with configurable override via `FABRIC_MAX_ROWS` env var. Include total row count in response metadata when available.

**Rationale**: LLM context windows have practical limits. 500 rows is a reasonable default that balances data completeness with context consumption. The limit is applied via `TOP` clause injection or post-fetch truncation.

**Alternatives considered**:
- Client-specified limit per query: Adds parameter complexity. Can be added in v2.
- No limit: Risk of overwhelming LLM context.
