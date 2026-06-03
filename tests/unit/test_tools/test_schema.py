"""Unit tests for schema discovery tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.models import ColumnInfo


def _make_schema_tools(db: MagicMock | None = None) -> dict[str, callable]:
    """Create schema tool functions with mocked dependencies."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.schema import register_schema_tools

    mock_mcp = FastMCP("test")
    mock_db = db or MagicMock()
    register_schema_tools(mock_mcp, mock_db)

    tools = {}
    for tool in mock_mcp._tool_manager._tools.values():
        tools[tool.name] = (tool.fn, mock_db)
    return tools


class TestFabricListSchemas:
    """Test the list schemas tool."""

    def test_returns_non_system_schemas(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_list_schemas"]
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="SCHEMA_NAME", type="str", nullable=False)],
            [
                {"SCHEMA_NAME": "raw"},
                {"SCHEMA_NAME": "gold"},
                {"SCHEMA_NAME": "sys"},
                {"SCHEMA_NAME": "INFORMATION_SCHEMA"},
                {"SCHEMA_NAME": "guest"},
            ],
        )

        result = json.loads(fn())

        schema_names = [s["schema_name"] for s in result["schemas"]]
        assert "raw" in schema_names
        assert "gold" in schema_names
        assert "sys" not in schema_names
        assert "INFORMATION_SCHEMA" not in schema_names
        assert "guest" not in schema_names

    def test_empty_warehouse(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_list_schemas"]
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="SCHEMA_NAME", type="str", nullable=False)],
            [{"SCHEMA_NAME": "sys"}, {"SCHEMA_NAME": "INFORMATION_SCHEMA"}],
        )

        result = json.loads(fn())
        assert result["schemas"] == []


class TestFabricListTables:
    """Test the list tables tool."""

    def test_returns_all_tables(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_list_tables"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="TABLE_NAME", type="str", nullable=False),
                ColumnInfo(name="TABLE_TYPE", type="str", nullable=False),
            ],
            [
                {"TABLE_SCHEMA": "gold", "TABLE_NAME": "transactions", "TABLE_TYPE": "BASE TABLE"},
                {"TABLE_SCHEMA": "raw", "TABLE_NAME": "imports", "TABLE_TYPE": "BASE TABLE"},
            ],
        )

        result = json.loads(fn())

        assert len(result["tables"]) == 2
        assert result["tables"][0]["schema_name"] == "gold"

    def test_filter_by_schema(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_list_tables"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="TABLE_NAME", type="str", nullable=False),
                ColumnInfo(name="TABLE_TYPE", type="str", nullable=False),
            ],
            [{"TABLE_SCHEMA": "gold", "TABLE_NAME": "transactions", "TABLE_TYPE": "BASE TABLE"}],
        )

        result = json.loads(fn(schema_name="gold"))

        assert len(result["tables"]) == 1
        # Verify the SQL included the WHERE clause
        call_sql = mock_db.execute_query.call_args[0][0]
        assert "gold" in call_sql

    def test_empty_result(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_list_tables"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="TABLE_NAME", type="str", nullable=False),
                ColumnInfo(name="TABLE_TYPE", type="str", nullable=False),
            ],
            [],
        )

        result = json.loads(fn())
        assert result["tables"] == []


class TestFabricDescribeTable:
    """Test the describe table tool."""

    def test_schema_qualified_name(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="COLUMN_NAME", type="str", nullable=False),
                ColumnInfo(name="DATA_TYPE", type="str", nullable=False),
                ColumnInfo(name="IS_NULLABLE", type="str", nullable=False),
                ColumnInfo(name="CHARACTER_MAXIMUM_LENGTH", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_PRECISION", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_SCALE", type="int", nullable=True),
            ],
            [
                {
                    "TABLE_SCHEMA": "gold",
                    "COLUMN_NAME": "id",
                    "DATA_TYPE": "int",
                    "IS_NULLABLE": "NO",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": None,
                    "NUMERIC_SCALE": None,
                },
            ],
        )

        result = json.loads(fn("gold.transactions"))

        assert result["schema_name"] == "gold"
        assert result["table_name"] == "transactions"
        assert result["columns"][0]["name"] == "id"
        assert result["columns"][0]["nullable"] is False

    def test_unqualified_name(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="COLUMN_NAME", type="str", nullable=False),
                ColumnInfo(name="DATA_TYPE", type="str", nullable=False),
                ColumnInfo(name="IS_NULLABLE", type="str", nullable=False),
                ColumnInfo(name="CHARACTER_MAXIMUM_LENGTH", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_PRECISION", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_SCALE", type="int", nullable=True),
            ],
            [
                {
                    "TABLE_SCHEMA": "dbo",
                    "COLUMN_NAME": "col1",
                    "DATA_TYPE": "nvarchar",
                    "IS_NULLABLE": "YES",
                    "CHARACTER_MAXIMUM_LENGTH": 255,
                    "NUMERIC_PRECISION": None,
                    "NUMERIC_SCALE": None,
                },
            ],
        )

        result = json.loads(fn("test_table"))

        assert result["columns"][0]["type"] == "nvarchar(255)"
        assert result["columns"][0]["nullable"] is True

    def test_unqualified_name_ambiguous_across_schemas_rejected(self) -> None:
        """An unqualified name that exists in more than one schema must NOT
        silently merge columns from the different physical tables — it must
        return TABLE_AMBIGUOUS listing the candidate schemas so the caller
        can re-query with a schema qualifier (P5)."""
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False)],
            [
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "id", "DATA_TYPE": "int", "IS_NULLABLE": "NO",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0,
                },
                {
                    "TABLE_SCHEMA": "raw", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "raw_blob", "DATA_TYPE": "varchar",
                    "IS_NULLABLE": "YES", "CHARACTER_MAXIMUM_LENGTH": 4000,
                    "NUMERIC_PRECISION": None, "NUMERIC_SCALE": None,
                },
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "amount", "DATA_TYPE": "decimal",
                    "IS_NULLABLE": "YES", "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 2,
                },
            ],
        )

        result = json.loads(fn("transactions"))

        assert result["code"] == "TABLE_AMBIGUOUS", (
            f"unqualified name in multiple schemas must be rejected; got {result}"
        )
        # The candidate schemas must be disclosed so the caller can qualify.
        assert "gold" in result["message"] and "raw" in result["message"]
        assert "columns" not in result

    def test_unqualified_name_single_schema_multiple_columns_ok(self) -> None:
        """Regression guard: multiple columns of a single table (all the same
        TABLE_SCHEMA) must NOT be mistaken for cross-schema ambiguity."""
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False)],
            [
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "id", "DATA_TYPE": "int", "IS_NULLABLE": "NO",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 10, "NUMERIC_SCALE": 0,
                },
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "amount", "DATA_TYPE": "decimal",
                    "IS_NULLABLE": "YES", "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 2,
                },
            ],
        )

        result = json.loads(fn("transactions"))

        assert result.get("code") != "TABLE_AMBIGUOUS"
        assert result["schema_name"] == "gold"
        assert [c["name"] for c in result["columns"]] == ["id", "amount"]

    def test_zero_scale_numeric_keeps_precision(self) -> None:
        """DECIMAL(p,0) / NUMERIC(p,0) have NUMERIC_SCALE == 0 (falsy). The
        precision/scale must still be rendered — bare 'decimal' loses the
        declared precision and misleads the caller (P7)."""
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False)],
            [
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "amount", "DATA_TYPE": "decimal",
                    "IS_NULLABLE": "NO", "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 0,
                },
                {
                    "TABLE_SCHEMA": "gold", "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "rate", "DATA_TYPE": "decimal",
                    "IS_NULLABLE": "NO", "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 18, "NUMERIC_SCALE": 6,
                },
            ],
        )

        result = json.loads(fn("gold.t"))
        types = {c["name"]: c["type"] for c in result["columns"]}
        assert types["amount"] == "decimal(18,0)", (
            f"zero-scale numeric must keep precision; got {types['amount']}"
        )
        assert types["rate"] == "decimal(18,6)"

    def test_table_not_found(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [
                ColumnInfo(name="TABLE_SCHEMA", type="str", nullable=False),
                ColumnInfo(name="COLUMN_NAME", type="str", nullable=False),
                ColumnInfo(name="DATA_TYPE", type="str", nullable=False),
                ColumnInfo(name="IS_NULLABLE", type="str", nullable=False),
                ColumnInfo(name="CHARACTER_MAXIMUM_LENGTH", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_PRECISION", type="int", nullable=True),
                ColumnInfo(name="NUMERIC_SCALE", type="int", nullable=True),
            ],
            [],
        )

        result = json.loads(fn("gold.nonexistent"))
        assert result["code"] == "TABLE_NOT_FOUND"

    def test_returns_object_type_for_base_table(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [],
            [
                {
                    "TABLE_SCHEMA": "gold",
                    "TABLE_TYPE": "BASE TABLE",
                    "COLUMN_NAME": "id",
                    "DATA_TYPE": "int",
                    "IS_NULLABLE": "NO",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": None,
                    "NUMERIC_SCALE": None,
                },
            ],
        )

        result = json.loads(fn("gold.transactions"))
        assert result["object_type"] == "BASE TABLE"

    def test_returns_object_type_for_view(self) -> None:
        tools = _make_schema_tools()
        fn, mock_db = tools["fabric_describe_table"]
        mock_db.execute_query.return_value = (
            [],
            [
                {
                    "TABLE_SCHEMA": "gold",
                    "TABLE_TYPE": "VIEW",
                    "COLUMN_NAME": "entity_id",
                    "DATA_TYPE": "int",
                    "IS_NULLABLE": "NO",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": None,
                    "NUMERIC_SCALE": None,
                },
                {
                    "TABLE_SCHEMA": "gold",
                    "TABLE_TYPE": "VIEW",
                    "COLUMN_NAME": "usd_amount",
                    "DATA_TYPE": "decimal",
                    "IS_NULLABLE": "YES",
                    "CHARACTER_MAXIMUM_LENGTH": None,
                    "NUMERIC_PRECISION": 18,
                    "NUMERIC_SCALE": 2,
                },
            ],
        )

        result = json.loads(fn("gold.vw_Sch1X_EntityUSD"))
        assert result["object_type"] == "VIEW"
        assert len(result["columns"]) == 2


class TestFabricListWritableTables:
    """The fabric_list_writable_tables tool reflects config.write_allowlist."""

    def _make_with_allowlist(self, allowlist: list[str]) -> callable:
        from mcp.server.fastmcp import FastMCP

        from src.config import FabricSettings
        from src.tools.write import register_write_tools

        cfg = FabricSettings(
            server="test.datawarehouse.fabric.microsoft.com",
            database="db",
            client_id="cid",
            client_secret="cs",
            tenant_id="tid",
            api_key="test-api-key",
            write_allowlist=allowlist,
        )
        mock_mcp = FastMCP("test")
        register_write_tools(mock_mcp, MagicMock(), cfg)
        for tool in mock_mcp._tool_manager._tools.values():
            if tool.name == "fabric_list_writable_tables":
                return tool.fn
        raise RuntimeError("fabric_list_writable_tables not registered")

    def test_returns_configured_allowlist(self) -> None:
        fn = self._make_with_allowlist(["raw.Fact_ExchangeRate", "gold.Dim_Entity"])
        result = json.loads(fn())
        assert set(result["writable_tables"]) == {"raw.Fact_ExchangeRate", "gold.Dim_Entity"}

    def test_empty_allowlist_returns_empty_list(self) -> None:
        fn = self._make_with_allowlist([])
        result = json.loads(fn())
        assert result["writable_tables"] == []
