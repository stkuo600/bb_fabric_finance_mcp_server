# Tasks: Fabric SQL MCP Server

**Input**: Design documents from `/specs/001-fabric-sql-mcp-server/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included per Constitution Principle II (test-first or test-alongside required).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

## User Story Mapping

| Story | Spec Story | Priority | Description |
|-------|-----------|----------|-------------|
| US3 | User Story 3 | P1 | Server Configuration and Connection (Foundational) |
| US1 | User Story 1 | P1 | Query Data from Fabric Warehouse (MVP) |
| US4 | User Story 4 | P2 | Schema Discovery |
| US2 | User Story 2 | P2 | Insert and Update Data |

> US3 is foundational — all other stories depend on it.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, dependencies, and tooling

- [x] T001 Create project directory structure: `src/`, `src/tools/`, `tests/unit/`, `tests/unit/test_tools/`, `tests/contract/`, `tests/integration/`
- [x] T002 Create `pyproject.toml` with dependencies: mcp, pyodbc, msal, pydantic, pydantic-settings; dev dependencies: pytest, pytest-asyncio, pytest-cov, ruff
- [x] T003 [P] Configure ruff linting and formatting in `pyproject.toml` (ruff section)
- [x] T004 [P] Create `.gitignore` with Python defaults, `config.json`, `.env`, `__pycache__/`, `.pytest_cache/`
- [x] T005 [P] Create `src/__init__.py` and `src/tools/__init__.py` package files
- [x] T006 Install dependencies and generate lock file

---

## Phase 2: Foundational — US3 Configuration & Connection (Priority: P1)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**Goal**: Server starts, authenticates with Fabric, and establishes a database connection

**Independent Test**: Start the server with valid config and verify it authenticates and responds to MCP protocol introspection

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for Foundational

- [x] T007 [P] Unit tests for config loading (env vars, config file, precedence, validation) in `tests/unit/test_config.py`
- [x] T008 [P] Unit tests for auth token acquisition and refresh in `tests/unit/test_auth.py`
- [x] T009 [P] Unit tests for database connection management in `tests/unit/test_database.py`

### Implementation

- [x] T010 [P] Define all Pydantic models in `src/models.py`: FabricConfig, QueryResult, ColumnInfo, WriteResult, WritePreview, TableInfo, ErrorResponse per data-model.md
- [x] T011 [P] Implement structured JSON logging setup in `src/logging_setup.py` with DEBUG/INFO/WARNING/ERROR/CRITICAL levels
- [x] T012 Implement configuration loading in `src/config.py`: Pydantic BaseSettings with `FABRIC_` env var prefix, JSON config file fallback, validation rules per data-model.md
- [x] T013 Implement MSAL OAuth2 authentication in `src/auth.py`: ConfidentialClientApplication, acquire_token_silent/acquire_token_for_client pattern, scope `https://analysis.windows.net/powerbi/api/.default`
- [x] T014 Implement database connection manager in `src/database.py`: pyodbc connection via ODBC Driver 18, token passed via SQL_COPT_SS_ACCESS_TOKEN (attr 1256, UTF-16LE encoding), autocommit=True, query execution with timeout, error handling returning ErrorResponse
- [x] T015 Create MCP server skeleton in `src/server.py`: FastMCP setup with stateless_http=True and json_response=True, streamable-http transport, config loading at startup, auth + db initialization, `__main__` entry point
- [x] T016 Verify server starts and responds to MCP protocol introspection (tools/list)

**Checkpoint**: Server starts, authenticates with Fabric, and responds to MCP introspection. Foundation ready.

---

## Phase 3: User Story 1 — Query Data (Priority: P1) 🎯 MVP

**Goal**: LLM users can execute SELECT queries and receive structured JSON results

**Independent Test**: Send a SELECT query through the MCP server and verify correct JSON array of objects is returned

### Tests for User Story 1

- [x] T017 [P] [US1] Unit tests for query tool in `tests/unit/test_tools/test_query.py`: SELECT execution, empty results, invalid SQL error, non-SELECT rejection, row limit truncation
- [x] T018 [P] [US1] Contract test for `fabric_execute_query` tool schema in `tests/contract/test_mcp_tools.py`: verify tool name, parameter schema, return structure matches contracts/mcp-tools.md

### Implementation for User Story 1

- [x] T019 [US1] Implement `fabric_execute_query` tool in `src/tools/query.py`: accept SQL string, validate is SELECT, execute via database.py, return QueryResult as JSON array of objects, apply max_rows limit with truncated flag, 30s query timeout, error handling returning ErrorResponse with codes QUERY_ERROR/INVALID_OPERATION
- [x] T020 [US1] Register `fabric_execute_query` tool in `src/server.py` via @mcp.tool() decorator
- [x] T021 [US1] Add structured logging for query operations in `src/tools/query.py`: log query execution, row count, truncation, errors

**Checkpoint**: User Story 1 fully functional — SELECT queries work end-to-end. This is the MVP.

---

## Phase 4: User Story 4 — Schema Discovery (Priority: P2)

**Goal**: LLM users can list schemas, list tables (optionally filtered by schema), and describe table columns

**Independent Test**: Request schema list and table details, verify they match the actual warehouse structure

### Tests for User Story 4

- [x] T022 [P] [US4] Unit tests for schema tools in `tests/unit/test_tools/test_schema.py`: list_schemas (excludes system schemas), list_tables (all and filtered), describe_table (with/without schema qualifier, not found error)
- [x] T023 [P] [US4] Contract tests for `fabric_list_schemas`, `fabric_list_tables`, `fabric_describe_table` tool schemas in `tests/contract/test_mcp_tools.py`

### Implementation for User Story 4

- [x] T024 [P] [US4] Implement `fabric_list_schemas` tool in `src/tools/schema.py`: query INFORMATION_SCHEMA.SCHEMATA excluding system schemas (sys, INFORMATION_SCHEMA, guest, db_owner, db_accessadmin, db_securityadmin, db_ddladmin, db_backupoperator, db_datareader, db_datawriter, db_denydatareader, db_denydatawriter)
- [x] T025 [P] [US4] Implement `fabric_list_tables` tool in `src/tools/schema.py`: query INFORMATION_SCHEMA.TABLES with optional schema_name filter parameter, return list of TableInfo
- [x] T026 [US4] Implement `fabric_describe_table` tool in `src/tools/schema.py`: parse optional schema qualifier from table_name, query INFORMATION_SCHEMA.COLUMNS, return column details with name/type/nullable, TABLE_NOT_FOUND error if not found
- [x] T027 [US4] Register `fabric_list_schemas`, `fabric_list_tables`, `fabric_describe_table` tools in `src/server.py`
- [x] T028 [US4] Add structured logging for schema discovery operations in `src/tools/schema.py`

**Checkpoint**: User Stories 1 AND 4 both work independently. LLM can discover schemas/tables then query data.

---

## Phase 5: User Story 2 — Insert and Update Data (Priority: P2)

**Goal**: LLM users can execute INSERT/UPDATE with two-phase confirmation (preview + execute) and table allowlist enforcement

**Independent Test**: Submit an INSERT/UPDATE, receive preview with token, confirm with token, verify data is written and affected row count returned

### Tests for User Story 2

- [x] T029 [P] [US2] Unit tests for write preview tool in `tests/unit/test_tools/test_write.py`: valid INSERT/UPDATE preview, table not on allowlist rejection, non-INSERT/UPDATE rejection, SQL parsing for table extraction
- [x] T030 [P] [US2] Unit tests for write execute tool in `tests/unit/test_tools/test_write.py`: valid token execution, expired token error, invalid/used token error, one-time use enforcement
- [x] T031 [P] [US2] Contract tests for `fabric_preview_write` and `fabric_execute_write` tool schemas in `tests/contract/test_mcp_tools.py`

### Implementation for User Story 2

- [x] T032 [US2] Implement confirmation token store in `src/tools/write.py`: in-memory dict mapping UUID to pending SQL + metadata, 5-minute expiry, one-time use invalidation
- [x] T033 [US2] Implement `fabric_preview_write` tool in `src/tools/write.py`: parse SQL to extract operation type (INSERT/UPDATE) and target table, validate against write_allowlist from config, generate UUID confirmation token, return WritePreview with sql_summary and expires_at, error handling for TABLE_NOT_ALLOWED/INVALID_OPERATION
- [x] T034 [US2] Implement `fabric_execute_write` tool in `src/tools/write.py`: validate confirmation_token exists and not expired, execute stored SQL via database.py, return WriteResult with affected_rows, invalidate token after use, error handling for TOKEN_EXPIRED/TOKEN_INVALID
- [x] T035 [US2] Register `fabric_preview_write` and `fabric_execute_write` tools in `src/server.py`
- [x] T036 [US2] Add structured logging for write operations in `src/tools/write.py`: log preview requests, confirmations, executions, rejections

**Checkpoint**: All user stories functional. Full read + write + schema discovery capabilities.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [x] T037 [P] Add integration test for full Fabric connection flow in `tests/integration/test_fabric_connection.py`: config → auth → connect → query → schema discovery → write preview/execute
- [x] T038 [P] Create `config.example.json` with placeholder values for developer onboarding
- [x] T039 [P] Create `Dockerfile` for Azure Container Apps deployment: Python 3.11 slim, ODBC Driver 18 install, pip install, expose port
- [x] T040 Run all unit and contract tests, verify >= 80% coverage
- [x] T041 Run quickstart.md validation: verify setup steps work end-to-end
- [x] T042 Final code review: verify Constitution compliance (code quality, naming conventions, error structure, logging, type annotations)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup (T001-T006) — BLOCKS all user stories
- **US1 Query (Phase 3)**: Depends on Foundational (Phase 2) completion
- **US4 Schema (Phase 4)**: Depends on Foundational (Phase 2) completion — independent of US1
- **US2 Write (Phase 5)**: Depends on Foundational (Phase 2) completion — independent of US1/US4
- **Polish (Phase 6)**: Depends on all desired user stories being complete

### User Story Dependencies

- **US3 (Foundational)**: No dependencies on other stories — prerequisite for all
- **US1 (Query)**: Depends only on Foundational — no cross-story dependencies
- **US4 (Schema)**: Depends only on Foundational — no cross-story dependencies
- **US2 (Write)**: Depends only on Foundational — no cross-story dependencies

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Models/infrastructure before tool implementation
- Tool implementation before server registration
- Core logic before logging/polish

### Parallel Opportunities

- T003, T004, T005 can run in parallel (Phase 1)
- T007, T008, T009 can run in parallel (Foundational tests)
- T010, T011 can run in parallel (models + logging)
- T017, T018 can run in parallel (US1 tests)
- T022, T023 can run in parallel (US4 tests)
- T024, T025 can run in parallel (US4 schema tools)
- T029, T030, T031 can run in parallel (US2 tests)
- T037, T038, T039 can run in parallel (Polish)
- **After Foundational**: US1, US4, US2 can all start in parallel if team capacity allows

---

## Parallel Example: User Story 1

```bash
# Launch US1 tests together:
Task: "Unit tests for query tool in tests/unit/test_tools/test_query.py"
Task: "Contract test for fabric_execute_query in tests/contract/test_mcp_tools.py"

# After tests written, implement:
Task: "Implement fabric_execute_query tool in src/tools/query.py"
```

## Parallel Example: User Story 4

```bash
# Launch US4 tests together:
Task: "Unit tests for schema tools in tests/unit/test_tools/test_schema.py"
Task: "Contract tests for schema tools in tests/contract/test_mcp_tools.py"

# After tests, implement in parallel:
Task: "Implement fabric_list_schemas in src/tools/schema.py"
Task: "Implement fabric_list_tables in src/tools/schema.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (US3 — config + auth + db connection)
3. Complete Phase 3: User Story 1 (query tool)
4. **STOP and VALIDATE**: Test SELECT queries independently
5. Deploy/demo if ready — server can query Fabric warehouse

### Incremental Delivery

1. Setup + Foundational → Server starts and connects to Fabric
2. Add US1 (Query) → Test independently → Deploy/Demo **(MVP!)**
3. Add US4 (Schema Discovery) → Test independently → Deploy/Demo
4. Add US2 (Write) → Test independently → Deploy/Demo
5. Each story adds value without breaking previous stories

### Parallel Team Strategy

With multiple developers after Foundational phase:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: User Story 1 (Query)
   - Developer B: User Story 4 (Schema Discovery)
   - Developer C: User Story 2 (Write)
3. Stories complete and integrate independently

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story is independently completable and testable
- Tests must fail before implementing (Constitution Principle II)
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- Constitution requires >= 80% line coverage, structured JSON logging, uniform error codes
