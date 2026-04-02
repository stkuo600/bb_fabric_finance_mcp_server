"""Pydantic models for the Fabric SQL MCP Server."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FabricConfig(BaseModel):
    """Connection and behavior configuration for the Fabric SQL endpoint."""

    server: str = Field(description="Fabric SQL endpoint hostname")
    database: str = Field(description="Warehouse database name")
    client_id: str = Field(description="Azure AD app client ID")
    client_secret: str = Field(description="Azure AD app client secret")
    tenant_id: str = Field(description="Azure AD tenant ID")
    write_allowlist: list[str] = Field(
        default_factory=list,
        description="Tables permitted for write operations (empty = no writes allowed)",
    )
    max_rows: int = Field(default=500, ge=1, le=10000, description="Max rows returned per query")
    port: int = Field(default=8000, description="HTTP server port")


class ColumnInfo(BaseModel):
    """Column metadata for a query result or table description."""

    name: str
    type: str
    nullable: bool


class QueryResult(BaseModel):
    """Output of a SELECT query."""

    columns: list[ColumnInfo]
    rows: list[dict[str, object]]
    row_count: int
    truncated: bool


class WriteResult(BaseModel):
    """Output of an INSERT/UPDATE operation."""

    affected_rows: int
    operation: str
    table: str


class WritePreview(BaseModel):
    """Preview returned before write confirmation."""

    confirmation_token: str
    operation: str
    table: str
    sql_summary: str
    expires_at: str


class TableInfo(BaseModel):
    """Metadata for a database table or view."""

    schema_name: str
    table_name: str
    table_type: str


class SchemaInfo(BaseModel):
    """Metadata for a database schema."""

    schema_name: str


class ErrorResponse(BaseModel):
    """Uniform error structure per Constitution Principle III."""

    code: str
    message: str
    details: str | None = None
