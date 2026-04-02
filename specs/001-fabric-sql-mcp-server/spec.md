# Feature Specification: Fabric SQL MCP Server

**Feature Branch**: `001-fabric-sql-mcp-server`  
**Created**: 2026-04-02  
**Status**: Draft  
**Input**: User description: "建立一個 HTTP MCP server for LLM 可以對 Microsoft Fabric 的 data warehouse 查詢資料(執行 SQL 語句) 或是新增更新資料, 使用 SQL End Point 連線"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Query Data from Fabric Warehouse (Priority: P1)

As an LLM user, I want to execute read-only SQL queries against a Microsoft Fabric data warehouse so that I can retrieve and analyze data through natural language interactions.

**Why this priority**: Querying data is the most fundamental and frequently used operation. Without it, the server provides no value.

**Independent Test**: Can be fully tested by sending a SELECT query through the MCP server and verifying correct results are returned from the Fabric warehouse.

**Acceptance Scenarios**:

1. **Given** a properly configured MCP server with valid Fabric credentials, **When** a user sends a SELECT SQL query, **Then** the server returns the query results in a structured format with column names and row data.
2. **Given** a query that returns no rows, **When** the query is executed, **Then** the server returns an empty result set with column metadata.
3. **Given** an invalid SQL query, **When** the query is submitted, **Then** the server returns a clear error message describing what went wrong.
4. **Given** valid credentials but an unreachable Fabric server, **When** a query is submitted, **Then** the server returns a connection error with actionable guidance.

---

### User Story 2 - Insert and Update Data in Fabric Warehouse (Priority: P2)

As an LLM user, I want to execute INSERT and UPDATE SQL statements against the Fabric data warehouse so that I can modify data through natural language interactions.

**Why this priority**: Write operations extend the server's utility beyond read-only access, enabling full data management workflows.

**Independent Test**: Can be fully tested by sending INSERT/UPDATE statements and verifying the data changes are persisted in the Fabric warehouse.

**Acceptance Scenarios**:

1. **Given** a valid INSERT SQL statement, **When** submitted to the MCP server, **Then** the data is inserted into the target table and the server returns a confirmation with affected row count.
2. **Given** a valid UPDATE SQL statement, **When** submitted to the MCP server, **Then** the matching rows are updated and the server returns the number of affected rows.
3. **Given** an INSERT/UPDATE that violates a constraint (e.g., duplicate key, null violation), **When** submitted, **Then** the server returns a descriptive error without partial data corruption.

---

### User Story 3 - Server Configuration and Connection (Priority: P1)

As an administrator, I want to configure the MCP server with Fabric connection parameters so that it can authenticate and connect to the correct data warehouse.

**Why this priority**: Without proper configuration and authentication, no operations can be performed. This is a prerequisite for all other stories.

**Independent Test**: Can be fully tested by starting the server with valid configuration and verifying it successfully authenticates with Microsoft Fabric.

**Acceptance Scenarios**:

1. **Given** a configuration with server, database, client_id, client_secret, and tenant_id, **When** the MCP server starts, **Then** it authenticates and establishes a connection to the Fabric SQL endpoint.
2. **Given** invalid credentials (wrong client_secret or tenant_id), **When** the server attempts to connect, **Then** it returns a clear authentication error.
3. **Given** a missing required configuration parameter, **When** the server starts, **Then** it fails with a message indicating which parameter is missing.

---

### User Story 4 - Schema Discovery (Priority: P2)

As an LLM user, I want to list available tables and view their schemas so that I can understand what data is available before writing queries.

**Why this priority**: Schema discovery helps users formulate correct queries, reducing errors and improving the overall experience.

**Independent Test**: Can be fully tested by requesting a table list and schema details and verifying they match the actual warehouse structure.

**Acceptance Scenarios**:

1. **Given** an authenticated connection, **When** a user requests a list of tables, **Then** the server returns all available table names with their schemas.
2. **Given** a specific table name, **When** a user requests its schema, **Then** the server returns column names, data types, and nullable indicators.

---

### Edge Cases

- What happens when a query exceeds the Fabric SQL endpoint's timeout or row limit?
- How does the system handle concurrent requests from multiple LLM sessions?
- What happens when the authentication token expires mid-session?
- How does the system handle very large result sets that could overwhelm LLM context windows?
- What happens when the Fabric warehouse is paused or suspended?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST expose MCP tools over HTTP using the Model Context Protocol standard, allowing LLM clients to discover and invoke available operations.
- **FR-002**: System MUST authenticate with Microsoft Fabric using the provided tenant_id, client_id, and client_secret via standard credential flow.
- **FR-003**: System MUST connect to the specified Fabric data warehouse via its SQL endpoint using the provided server and database parameters.
- **FR-004**: System MUST provide a tool for executing read-only SQL queries (SELECT) and returning structured results as a JSON array of objects (e.g., `[{"col1": "val1", "col2": "val2"}, ...]`).
- **FR-005**: System MUST provide a tool for executing write SQL statements (INSERT, UPDATE) and returning affected row counts.
- **FR-011**: System MUST require explicit confirmation before executing any write operation — the write tool first returns a preview/warning of the intended change, and only executes after the caller confirms.
- **FR-012**: System MUST support a configurable allowlist of tables permitted for write operations. Write statements targeting tables not on the allowlist MUST be rejected with a clear error.
- **FR-006**: System MUST provide a tool for listing available tables and their column schemas.
- **FR-015**: System MUST provide a tool for listing available database schemas (e.g., raw, gold).
- **FR-007**: System MUST validate all configuration parameters at startup and fail with descriptive errors for missing or invalid values.
- **FR-013**: System MUST accept configuration via environment variables as the primary method (suitable for Azure Container Apps deployment).
- **FR-014**: System MUST also support loading configuration from a local config file for development convenience. Environment variables take precedence over config file values when both are present.
- **FR-008**: System MUST return clear, LLM-friendly error messages for SQL errors, connection failures, and authentication issues.
- **FR-009**: System MUST limit query result sizes to prevent overwhelming LLM context windows (reasonable default: 500 rows).
- **FR-010**: System MUST handle token refresh automatically when tokens expire.

### Key Entities

- **Connection Configuration**: Represents the Fabric connection parameters (server, database, client_id, client_secret, tenant_id) needed to establish and maintain the warehouse connection.
- **Query Result**: Represents the output of a SQL query, containing column metadata and row data in a structured format suitable for LLM consumption.
- **Table Schema**: Represents the structure of a warehouse table, including column names, data types, and constraints.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can execute a SQL query and receive results within 30 seconds for typical queries (under 1000 rows).
- **SC-002**: Server starts and authenticates with Fabric within 10 seconds given valid credentials.
- **SC-003**: 100% of invalid SQL statements return user-friendly error messages (no raw stack traces exposed).
- **SC-004**: Server operates continuously for 24+ hours without requiring manual token refresh or reconnection.
- **SC-005**: LLM clients can discover all available tools through standard MCP protocol introspection.

## Clarifications

### Session 2026-04-02

- Q: Should write operations have safety guardrails? → A: Both confirmation preview and configurable table allowlist required (Option D).
- Q: How should configuration be delivered? → A: Environment variables as primary (for Azure Container Apps), config file as fallback for local development. Env vars take precedence.
- Q: What format for query results returned to LLM? → A: JSON array of objects (e.g., `[{"col1": "val1"}, ...]`).

## Assumptions

- Users have an active Microsoft Fabric workspace with a provisioned data warehouse.
- The Fabric SQL endpoint is enabled and accessible from the network where the MCP server runs.
- An Azure AD app registration exists with appropriate permissions to access the Fabric data warehouse.
- The MCP server will be deployed as a single-instance service (horizontal scaling is out of scope for v1).
- DELETE and DDL operations (CREATE TABLE, ALTER, DROP) are out of scope for v1 to minimize risk of destructive operations.
- The server will use HTTP transport (not stdio) as specified in the feature description.
- Result set row limits and query timeouts will use sensible defaults, configurable if needed.
