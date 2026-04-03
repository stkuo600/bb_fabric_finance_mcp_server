"""MCP tools for schema discovery in Fabric data warehouse."""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

from src.database import FabricDatabase
from src.models import ColumnInfo, ErrorResponse, SchemaInfo, TableInfo

logger = logging.getLogger("fabric_mcp.tools.schema")

_SYSTEM_SCHEMAS = {
    "sys",
    "INFORMATION_SCHEMA",
    "guest",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
}


def register_schema_tools(mcp: FastMCP, db: FabricDatabase) -> None:
    """Register schema discovery MCP tools."""

    @mcp.tool()
    def fabric_list_schemas() -> str:
        """List all database schemas in the connected Fabric data warehouse.

        Returns a list of schema names, excluding system schemas.
        Useful for understanding the warehouse structure before querying tables.
        """
        logger.info("List schemas requested", extra={"tool": "fabric_list_schemas"})
        sql = "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
        try:
            _, rows = db.execute_query(sql)
        except RuntimeError as e:
            logger.error("List schemas failed", extra={"tool": "fabric_list_schemas"})
            return str(e)

        schemas = [
            SchemaInfo(schema_name=row["SCHEMA_NAME"]).model_dump()
            for row in rows
            if row["SCHEMA_NAME"] not in _SYSTEM_SCHEMAS
        ]
        logger.info("Listed %d schemas", len(schemas), extra={"tool": "fabric_list_schemas"})
        return json.dumps({"schemas": schemas})

    @mcp.tool()
    def fabric_list_tables(schema_name: str | None = None) -> str:
        """List all tables and views in the connected Fabric data warehouse.

        Optionally filter by schema name. Returns table names with their schema and type.

        Args:
            schema_name: Filter tables by schema (e.g., "gold"). If omitted, returns all schemas.
        """
        logger.info(
            "List tables requested",
            extra={"tool": "fabric_list_tables", "table": schema_name or "all"},
        )
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
            "FROM INFORMATION_SCHEMA.TABLES "
        )
        if schema_name:
            sql += f"WHERE TABLE_SCHEMA = '{schema_name}' "
        sql += "ORDER BY TABLE_SCHEMA, TABLE_NAME"

        try:
            _, rows = db.execute_query(sql)
        except RuntimeError as e:
            logger.error("List tables failed", extra={"tool": "fabric_list_tables"})
            return str(e)

        tables = [
            TableInfo(
                schema_name=row["TABLE_SCHEMA"],
                table_name=row["TABLE_NAME"],
                table_type=row["TABLE_TYPE"],
            ).model_dump()
            for row in rows
        ]
        logger.info("Listed %d tables", len(tables), extra={"tool": "fabric_list_tables"})
        return json.dumps({"tables": tables})

    @mcp.tool()
    def fabric_describe_table(table_name: str) -> str:
        """Get column details for a specific table in the Fabric data warehouse.

        Accepts table names with or without schema qualifier (e.g., "gold.transactions" or "transactions").

        Args:
            table_name: Table name, optionally schema-qualified (e.g., "gold.transactions").
        """
        logger.info("Describe table requested", extra={"tool": "fabric_describe_table", "table": table_name})

        if "." in table_name:
            schema, table = table_name.split(".", 1)
        else:
            schema = None
            table = table_name

        sql = (
            "SELECT TABLE_SCHEMA, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
            "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
            f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}'"
        )
        if schema:
            sql += f" AND TABLE_SCHEMA = '{schema}'"
        sql += " ORDER BY ORDINAL_POSITION"

        try:
            _, rows = db.execute_query(sql)
        except RuntimeError as e:
            logger.error("Describe table failed", extra={"tool": "fabric_describe_table"})
            return str(e)

        if not rows:
            error = ErrorResponse(
                code="TABLE_NOT_FOUND",
                message=f"Table '{table_name}' not found in the data warehouse",
            )
            logger.warning("Table not found: %s", table_name, extra={"tool": "fabric_describe_table"})
            return error.model_dump_json()

        resolved_schema = rows[0]["TABLE_SCHEMA"]

        columns = []
        for row in rows:
            data_type = row["DATA_TYPE"]
            if row.get("CHARACTER_MAXIMUM_LENGTH"):
                data_type = f"{data_type}({row['CHARACTER_MAXIMUM_LENGTH']})"
            elif row.get("NUMERIC_PRECISION") and row.get("NUMERIC_SCALE"):
                data_type = f"{data_type}({row['NUMERIC_PRECISION']},{row['NUMERIC_SCALE']})"
            columns.append(
                ColumnInfo(
                    name=row["COLUMN_NAME"],
                    type=data_type,
                    nullable=row["IS_NULLABLE"] == "YES",
                ).model_dump()
            )

        result = {
            "schema_name": resolved_schema,
            "table_name": table,
            "columns": columns,
        }
        logger.info(
            "Described table %s: %d columns",
            table_name,
            len(columns),
            extra={"tool": "fabric_describe_table", "table": table_name},
        )
        return json.dumps(result)
