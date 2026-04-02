# Implementation Plan: Fabric SQL MCP Server

**Branch**: `001-fabric-sql-mcp-server` | **Date**: 2026-04-02 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-fabric-sql-mcp-server/spec.md`

## Summary

Build an HTTP-based MCP server in Python that connects to a Microsoft Fabric data warehouse via its SQL endpoint, exposing tools for querying data (SELECT), modifying data (INSERT/UPDATE with confirmation + table allowlist), and discovering schemas. Authentication uses OAuth2 client credentials via MSAL. Configuration is delivered via environment variables (production/ACA) with config file fallback (development).

## Technical Context

**Language/Version**: Python 3.11+  
**Primary Dependencies**: `mcp` (MCP Python SDK, streamable-http transport), `pyodbc` (ODBC Driver 18 for SQL Server), `msal` (Microsoft Authentication Library), `pydantic` (config validation)  
**Storage**: Microsoft Fabric Data Warehouse via SQL endpoint (external, no local storage)  
**Testing**: `pytest`, `pytest-asyncio`, `pytest-cov`  
**Target Platform**: Linux container (Azure Container Apps), Windows for local development  
**Project Type**: web-service (HTTP MCP server)  
**Performance Goals**: Tool responses within 10s p95 for external I/O operations; startup within 5s  
**Constraints**: Memory < 512 MB, single-instance deployment  
**Scale/Scope**: Single data warehouse connection, single-instance, interactive LLM usage patterns

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How Satisfied |
|-----------|--------|---------------|
| I. Code Quality | PASS | Single-responsibility modules, type annotations on all public interfaces, functions < 50 lines, files < 400 lines. Pre-commit hooks for linting/formatting (ruff). |
| II. Testing Standards | PASS | Test-first development. Unit tests for all public functions (>=80% coverage). Contract tests for MCP tool schemas. Integration tests with real Fabric connection or officially supported test doubles. |
| III. User Experience Consistency | PASS | Tool naming follows `fabric_<verb>_<noun>` pattern (adapting `<domain>_<verb>_<noun>` to this project). Uniform error structure with `code`, `message`, `details`. Snake_case parameters. |
| IV. Performance Requirements | PASS | External I/O operations target 10s p95 (Fabric queries). Startup < 5s. Memory < 512 MB. Timeout + retry with exponential backoff for Fabric connections. |
| Dev Standards - Secrets | PASS | No credentials in source. Env vars for production, config file (gitignored) for dev. |
| Dev Standards - Logging | PASS | Structured JSON logging for all operational events. |
| Dev Standards - Dependencies | PASS | All dependencies pinned in lock file (uv.lock or pip-tools). |

## Project Structure

### Documentation (this feature)

```text
specs/001-fabric-sql-mcp-server/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
src/
├── server.py            # FastMCP server setup, tool registration, entry point
├── config.py            # Configuration loading (env vars + config file fallback)
├── auth.py              # MSAL OAuth2 token acquisition and refresh
├── database.py          # pyodbc connection management, query execution
├── tools/
│   ├── query.py         # fabric_execute_query tool (SELECT)
│   ├── write.py         # fabric_execute_write tool (INSERT/UPDATE with confirmation)
│   └── schema.py        # fabric_list_schemas, fabric_list_tables, fabric_describe_table tools
├── models.py            # Pydantic models for config, results, errors
└── logging_setup.py     # Structured JSON logging configuration

tests/
├── unit/
│   ├── test_config.py
│   ├── test_auth.py
│   ├── test_database.py
│   └── test_tools/
│       ├── test_query.py
│       ├── test_write.py
│       └── test_schema.py
├── contract/
│   └── test_mcp_tools.py   # MCP tool schema contract tests
└── integration/
    └── test_fabric_connection.py
```

**Structure Decision**: Single-project layout. This is a focused MCP server with no frontend component. The `src/tools/` directory separates tool definitions for clarity while keeping the project flat.

## Complexity Tracking

> No constitution violations requiring justification.
