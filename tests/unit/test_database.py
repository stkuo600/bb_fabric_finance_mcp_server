"""Unit tests for database connection management."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

from src.database import FabricDatabase, _build_token_bytes


class TestBuildTokenBytes:
    """Test token encoding for pyodbc."""

    def test_encodes_token_as_utf16le_with_length_prefix(self) -> None:
        token = "test-token"
        result = _build_token_bytes(token)
        encoded = token.encode("UTF-16-LE")
        expected_length = struct.pack("<I", len(encoded))
        assert result[:4] == expected_length
        assert result[4:] == encoded

    def test_empty_token(self) -> None:
        result = _build_token_bytes("")
        assert result == struct.pack("<I", 0)


class TestFabricDatabase:
    """Test database connection and query execution."""

    def _make_db(self) -> tuple[FabricDatabase, MagicMock]:
        mock_auth = MagicMock()
        mock_auth.get_token.return_value = "test-access-token"
        db = FabricDatabase(
            server="test.datawarehouse.fabric.microsoft.com",
            database="gold_warehouse",
            auth=mock_auth,
        )
        return db, mock_auth

    @patch("src.database.pyodbc.connect")
    def test_execute_query_returns_columns_and_rows(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()

        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id", int, None, None, None, None, False),
            ("name", str, None, None, None, None, True),
        ]
        mock_cursor.fetchall.return_value = [(1, "Alice"), (2, "Bob")]
        mock_connect.return_value.cursor.return_value = mock_cursor

        columns, rows = db.execute_query("SELECT id, name FROM test")

        assert len(columns) == 2
        assert columns[0].name == "id"
        assert columns[1].nullable is True
        assert len(rows) == 2
        assert rows[0] == {"id": 1, "name": "Alice"}

    @patch("src.database.pyodbc.connect")
    def test_execute_query_empty_result(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()

        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = mock_cursor

        columns, rows = db.execute_query("SELECT id FROM empty_table")

        assert len(columns) == 1
        assert len(rows) == 0

    @patch("src.database.pyodbc.connect")
    def test_execute_query_raises_on_pyodbc_error(self, mock_connect: MagicMock) -> None:
        import pyodbc

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error("HY000", "Test error")

        with pytest.raises(RuntimeError, match="QUERY_ERROR"):
            db.execute_query("SELECT bad")

    @patch("src.database.pyodbc.connect")
    def test_execute_write_returns_affected_count(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()

        mock_cursor = MagicMock()
        mock_cursor.rowcount = 3
        mock_connect.return_value.cursor.return_value = mock_cursor

        result = db.execute_write("UPDATE test SET col=1")
        assert result == 3

    @patch("src.database.pyodbc.connect")
    def test_connection_uses_token_auth(self, mock_connect: MagicMock) -> None:
        db, mock_auth = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = mock_cursor

        db.execute_query("SELECT 1")

        mock_auth.get_token.assert_called_once()
        call_kwargs = mock_connect.call_args
        assert "attrs_before" in call_kwargs.kwargs
        assert 1256 in call_kwargs.kwargs["attrs_before"]

    @patch("src.database.pyodbc.connect")
    def test_connection_uses_autocommit(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = mock_cursor

        db.execute_query("SELECT 1")

        call_kwargs = mock_connect.call_args
        assert call_kwargs.kwargs["autocommit"] is True
