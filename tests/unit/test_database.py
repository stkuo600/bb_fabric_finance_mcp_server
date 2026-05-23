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
    def test_execute_query_raises_fabric_query_error_with_typed_fields(
        self, mock_connect: MagicMock
    ) -> None:
        """Typed exception carries structured payload — no JSON-string-in-message
        hack. Replaces the prior RuntimeError(model_dump_json()) contract."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42S02", "Invalid object name 'foo'"
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT * FROM foo")

        e = exc_info.value
        assert e.code == "QUERY_ERROR"
        assert "Invalid object name" in e.message
        assert e.details is None  # 42S02 does not match any Fabric-hint pattern
        assert e.sqlstate == "42S02"

    @patch("src.database.pyodbc.connect")
    def test_query_error_without_known_fabric_pattern_has_no_hint(self, mock_connect: MagicMock) -> None:
        """For non-matching errors, `details` stays None — no false hints."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42S02", "Invalid object name 'foo'"
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT * FROM foo")
        assert exc_info.value.code == "QUERY_ERROR"
        assert exc_info.value.details is None
        assert exc_info.value.sqlstate == "42S02"

    @patch("src.database.pyodbc.connect")
    def test_query_error_identity_overflow_carries_fabric_hint(self, mock_connect: MagicMock) -> None:
        """Fabric Warehouse cannot widen an existing INT IDENTITY column;
        the hint nudges the caller toward the recreate-with-BIGINT workaround."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "22003",
            "Arithmetic overflow error converting IDENTITY to data type int.",
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT 1")
        e = exc_info.value
        assert e.code == "QUERY_ERROR"
        assert e.details is not None
        assert "BIGINT" in e.details
        # Original error must be preserved in `message`
        assert "Arithmetic overflow" in e.message

    @patch("src.database.pyodbc.connect")
    def test_query_error_alter_table_add_column_carries_fabric_hint(self, mock_connect: MagicMock) -> None:
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42000",
            "ALTER TABLE ADD COLUMN is not supported on Fabric Warehouse.",
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT 1")
        e = exc_info.value
        assert e.details is not None
        assert "recreate" in e.details.lower() or "DROP" in e.details
        assert "ALTER TABLE ADD COLUMN" in e.message

    @patch("src.database.pyodbc.connect")
    def test_write_error_propagates_fabric_hint(self, mock_connect: MagicMock) -> None:
        """The hint logic must apply to execute_write too, not just queries."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "22003",
            "Arithmetic overflow error converting IDENTITY to data type int.",
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_write("INSERT INTO t VALUES (1)")
        e = exc_info.value
        assert e.details is not None
        assert "BIGINT" in e.details

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

        from src.database import FabricQueryError

        db, _ = self._make_db()

        # Single connection that returns different cursors on successive calls.
        bad_cursor = MagicMock()
        bad_cursor.execute.side_effect = pyodbc.Error("42S02", "Invalid object name 'no_such_table'")

        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = []

        mock_connect.return_value.cursor.side_effect = [bad_cursor, good_cursor]

        with pytest.raises(FabricQueryError):
            db.execute_query("SELECT * FROM no_such_table")

        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 1, (
            "query-side error (non-08 SQLSTATE) must not trigger a reconnect"
        )


class TestStaleConnectionRecovery:
    """Stale-connection recovery across the broader set of SQLSTATEs Fabric/MS-ODBC
    actually emits on idle disconnect — not just ISO `08*`.

    Reproduces `.claude/bugfix/2026-05-22-stale-conn-imc06-not-retried/repro.md`:
    Fabric drops idle TCP connections; the driver surfaces the failure as
    `HY000` (generic wrapper with TCP-layer message), `IMC06` (driver-side
    "unrecoverable" marker — emitted with no round trip on every subsequent
    call), or `HYT*` (connection timeout). The pre-fix classifier matched
    only `08*`, so these never triggered reconnect and the cached stale
    connection persisted indefinitely.
    """

    def _make_db(self) -> tuple[FabricDatabase, MagicMock]:
        mock_auth = MagicMock()
        mock_auth.get_token.return_value = "test-access-token"
        db = FabricDatabase(
            server="test.datawarehouse.fabric.microsoft.com",
            database="gold_warehouse",
            auth=mock_auth,
        )
        return db, mock_auth

    def _good_conn(self, rows: list[tuple[object, ...]] | None = None) -> MagicMock:
        conn = MagicMock(name="good_conn")
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = rows if rows is not None else []
        cursor.rowcount = 0
        conn.cursor.return_value = cursor
        return conn

    def _broken_conn(self, sqlstate: str, message: str) -> MagicMock:
        import pyodbc

        conn = MagicMock(name=f"broken_conn_{sqlstate}")
        cursor = MagicMock()
        cursor.execute.side_effect = pyodbc.Error(sqlstate, message)
        conn.cursor.return_value = cursor
        return conn

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_imc06_sqlstate(self, mock_connect: MagicMock) -> None:
        """IMC06 = client driver marked the connection unrecoverable.
        Microsoft docs: 'No attempt was made to restore the connection.'
        The MCP server must therefore discard and rebuild."""
        db, _ = self._make_db()
        broken = self._broken_conn(
            "IMC06",
            "[IMC06] [Microsoft][ODBC Driver 18 for SQL Server]"
            "The connection is broken and recovery is not possible. "
            "The connection is marked by the client driver as unrecoverable.",
        )
        good = self._good_conn(rows=[(42,)])
        mock_connect.side_effect = [broken, good]

        columns, rows = db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2, (
            "expected one reconnect after IMC06 (client-driver unrecoverable marker)"
        )
        assert rows == [{"id": 42}]
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_imc01_sqlstate(self, mock_connect: MagicMock) -> None:
        """IMC01 = driver tried ConnectRetryCount-bounded recovery and gave up.
        Application must still reconnect on the next call."""
        db, _ = self._make_db()
        broken = self._broken_conn(
            "IMC01",
            "[IMC01] The connection is broken and recovery is not possible. "
            "The client driver attempted to recover the connection one or more "
            "times and all attempts failed.",
        )
        good = self._good_conn()
        mock_connect.side_effect = [broken, good]

        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_hy000_with_communication_link_failure(
        self, mock_connect: MagicMock
    ) -> None:
        """HY000 with a TCP/communication-link message text — the generic
        wrapper Fabric/MS-ODBC sometimes surfaces on server-side disconnect."""
        db, _ = self._make_db()
        broken = self._broken_conn(
            "HY000",
            "[HY000] [Microsoft][ODBC Driver 18 for SQL Server]"
            "Communication link failure",
        )
        good = self._good_conn(rows=[(1,)])
        mock_connect.side_effect = [broken, good]

        _, rows = db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2, (
            "expected reconnect on HY000 carrying a connection-failure message"
        )
        assert rows == [{"id": 1}]
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_hy000_with_tcp_provider_reset(
        self, mock_connect: MagicMock
    ) -> None:
        """The user-reported error chain on the production incident:
        HY000 with TCP Provider 0x2746 (WSAECONNRESET)."""
        db, _ = self._make_db()
        broken = self._broken_conn(
            "HY000",
            "[HY000] [Microsoft][ODBC Driver 18 for SQL Server]"
            "[TCP Provider] Error code 0x2746 (10054)",
        )
        good = self._good_conn()
        mock_connect.side_effect = [broken, good]

        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_reconnects_on_hyt00_connection_timeout(
        self, mock_connect: MagicMock
    ) -> None:
        """HYT00 / HYT01 = connection or query timeout — connection state
        is suspect after a timeout, discard and reconnect."""
        db, _ = self._make_db()
        broken = self._broken_conn(
            "HYT00",
            "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Connection timeout expired",
        )
        good = self._good_conn()
        mock_connect.side_effect = [broken, good]

        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_write_reconnects_on_imc06(self, mock_connect: MagicMock) -> None:
        """execute_write must apply the same broadened classification."""
        db, _ = self._make_db()
        broken = MagicMock(name="broken")
        broken_cursor = MagicMock()
        import pyodbc

        broken_cursor.execute.side_effect = pyodbc.Error(
            "IMC06", "client driver marked unrecoverable"
        )
        broken.cursor.return_value = broken_cursor

        good = MagicMock(name="good")
        good_cursor = MagicMock()
        good_cursor.rowcount = 3
        good.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken, good]

        affected = db.execute_write("UPDATE t SET c = 1")

        assert affected == 3
        assert mock_connect.call_count == 2
        broken.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_genuine_query_error_still_does_not_reconnect(
        self, mock_connect: MagicMock
    ) -> None:
        """Regression guard for the broadened classifier — non-connection
        SQLSTATEs (42S02, 22003, etc.) must NOT trigger reconnect."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        bad_cursor = MagicMock()
        bad_cursor.execute.side_effect = pyodbc.Error(
            "42S02", "Invalid object name 'no_such_table'"
        )
        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.side_effect = [bad_cursor, good_cursor]

        with pytest.raises(FabricQueryError):
            db.execute_query("SELECT * FROM no_such_table")
        db.execute_query("SELECT 1")

        assert mock_connect.call_count == 1, (
            "syntax/object-name errors must not be misclassified as connection errors"
        )

    @patch("src.database.pyodbc.connect")
    def test_connection_string_enables_idle_resiliency(
        self, mock_connect: MagicMock
    ) -> None:
        """Defense in depth: driver-level idle resiliency must be enabled
        so most stale-connection cases never reach the application classifier."""
        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = cursor

        db.execute_query("SELECT 1")

        conn_string = mock_connect.call_args.args[0]
        assert "ConnectRetryCount" in conn_string, (
            "expected ConnectRetryCount keyword to enable driver-level "
            "idle connection resiliency (Microsoft Learn: 'Connection "
            "resiliency in the ODBC driver')"
        )
        assert "ConnectRetryInterval" in conn_string
