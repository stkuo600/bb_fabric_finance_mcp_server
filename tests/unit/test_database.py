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
    def test_query_error_without_known_fabric_pattern_has_no_hint(self, mock_connect: MagicMock) -> None:
        """For non-matching errors, `details` stays None — no false hints."""
        import json as _json

        import pyodbc

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42S02", "Invalid object name 'foo'"
        )

        try:
            db.execute_query("SELECT * FROM foo")
        except RuntimeError as e:
            payload = _json.loads(str(e))
            assert payload["code"] == "QUERY_ERROR"
            assert payload["details"] is None

    @patch("src.database.pyodbc.connect")
    def test_query_error_identity_overflow_carries_fabric_hint(self, mock_connect: MagicMock) -> None:
        """Fabric Warehouse cannot widen an existing INT IDENTITY column;
        the hint nudges the caller toward the recreate-with-BIGINT workaround."""
        import json as _json

        import pyodbc

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "22003",
            "Arithmetic overflow error converting IDENTITY to data type int.",
        )

        try:
            db.execute_query("SELECT 1")
        except RuntimeError as e:
            payload = _json.loads(str(e))
            assert payload["code"] == "QUERY_ERROR"
            assert payload["details"] is not None
            assert "BIGINT" in payload["details"]
            # Original error must be preserved in `message`
            assert "Arithmetic overflow" in payload["message"]

    @patch("src.database.pyodbc.connect")
    def test_query_error_alter_table_add_column_carries_fabric_hint(self, mock_connect: MagicMock) -> None:
        import json as _json

        import pyodbc

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42000",
            "ALTER TABLE ADD COLUMN is not supported on Fabric Warehouse.",
        )

        try:
            db.execute_query("SELECT 1")
        except RuntimeError as e:
            payload = _json.loads(str(e))
            assert payload["details"] is not None
            assert "recreate" in payload["details"].lower() or "DROP" in payload["details"]
            assert "ALTER TABLE ADD COLUMN" in payload["message"]

    @patch("src.database.pyodbc.connect")
    def test_write_error_propagates_fabric_hint(self, mock_connect: MagicMock) -> None:
        """The hint logic must apply to execute_write too, not just queries."""
        import json as _json

        import pyodbc

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "22003",
            "Arithmetic overflow error converting IDENTITY to data type int.",
        )

        try:
            db.execute_write("INSERT INTO t VALUES (1)")
        except RuntimeError as e:
            payload = _json.loads(str(e))
            assert payload["details"] is not None
            assert "BIGINT" in payload["details"]

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


class TestConnectionReuse:
    """Connection reuse across calls to amortise the pyodbc-connect handshake."""

    def _make_db(self) -> tuple[FabricDatabase, MagicMock]:
        mock_auth = MagicMock()
        mock_auth.get_token.return_value = "test-access-token"
        db = FabricDatabase(
            server="test.datawarehouse.fabric.microsoft.com",
            database="gold_warehouse",
            auth=mock_auth,
        )
        return db, mock_auth

    def _stub_cursor(self, mock_connect: MagicMock) -> MagicMock:
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = []
        cursor.rowcount = 0
        mock_connect.return_value.cursor.return_value = cursor
        return cursor

    @patch("src.database.pyodbc.connect")
    def test_execute_query_reuses_connection_across_calls(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()
        self._stub_cursor(mock_connect)

        for _ in range(5):
            db.execute_query("SELECT 1")

        assert mock_connect.call_count == 1, (
            f"expected one pyodbc.connect across 5 queries, got {mock_connect.call_count}"
        )

    @patch("src.database.pyodbc.connect")
    def test_execute_write_reuses_connection_across_calls(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()
        self._stub_cursor(mock_connect)

        for _ in range(5):
            db.execute_write("UPDATE t SET c=1")

        assert mock_connect.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_mixed_query_and_write_reuse_connection(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()
        self._stub_cursor(mock_connect)

        db.execute_query("SELECT 1")
        db.execute_write("UPDATE t SET c=1")
        db.execute_query("SELECT 2")
        db.execute_write("UPDATE t SET c=2")

        assert mock_connect.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_connection_class_sqlstate(self, mock_connect: MagicMock) -> None:
        import pyodbc

        db, _ = self._make_db()

        # Two distinct connection objects so we can distinguish "old" vs "new".
        broken_conn = MagicMock(name="broken_conn")
        broken_cursor = MagicMock()
        broken_cursor.execute.side_effect = pyodbc.Error("08S01", "Communication link failure")
        broken_conn.cursor.return_value = broken_cursor

        good_conn = MagicMock(name="good_conn")
        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = [(1,)]
        good_conn.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken_conn, good_conn]

        columns, rows = db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2, "expected one reconnect after 08-class SQLSTATE"
        assert len(rows) == 1
        assert rows[0] == {"id": 1}
        broken_conn.close.assert_called()  # broken connection must be discarded

    @patch("src.database.pyodbc.connect")
    def test_query_error_does_not_discard_connection(self, mock_connect: MagicMock) -> None:
        import pyodbc

        db, _ = self._make_db()

        # Single connection that returns different cursors on successive calls.
        bad_cursor = MagicMock()
        bad_cursor.execute.side_effect = pyodbc.Error("42S02", "Invalid object name 'no_such_table'")

        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = []

        mock_connect.return_value.cursor.side_effect = [bad_cursor, good_cursor]

        with pytest.raises(RuntimeError, match="QUERY_ERROR"):
            db.execute_query("SELECT * FROM no_such_table")

        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 1, (
            "query-side error (non-08 SQLSTATE) must not trigger a reconnect"
        )
