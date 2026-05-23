"""Unit tests for database connection management."""

from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

from src.database import FabricDatabase, _build_token_bytes, _is_connection_error


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


class TestExecuteWithRetry:
    """Direct tests of the retry seam — exercise the policy in isolation
    with a fake operation, so retry semantics can change without churning
    the cursor/description/fetchall scaffolding."""

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
    def test_calls_op_once_on_success(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()
        mock_connect.return_value = MagicMock(name="conn")

        calls: list[object] = []

        def op(conn: object) -> tuple[str, int]:
            calls.append(conn)
            return "result", 0

        result = db._execute_with_retry(op, op_label="test", sql_for_hash="SELECT 1")

        assert result == "result"
        assert len(calls) == 1
        assert mock_connect.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_retries_op_once_after_connection_class_error(
        self, mock_connect: MagicMock
    ) -> None:
        import pyodbc

        db, _ = self._make_db()
        broken_conn = MagicMock(name="broken_conn")
        good_conn = MagicMock(name="good_conn")
        mock_connect.side_effect = [broken_conn, good_conn]

        calls: list[object] = []

        def op(conn: object) -> tuple[str, int]:
            calls.append(conn)
            if len(calls) == 1:
                raise pyodbc.Error("IMC06", "connection broken")
            return "recovered", 0

        result = db._execute_with_retry(op, op_label="test", sql_for_hash="SELECT 1")

        assert result == "recovered"
        assert len(calls) == 2
        assert mock_connect.call_count == 2
        broken_conn.close.assert_called()  # stale conn discarded

    @patch("src.database.pyodbc.connect")
    def test_non_connection_error_raises_fabric_query_error_without_retry(
        self, mock_connect: MagicMock
    ) -> None:
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value = MagicMock(name="conn")

        calls: list[object] = []

        def op(conn: object) -> tuple[object, int]:
            calls.append(conn)
            raise pyodbc.Error("42S02", "Invalid object name 'x'")

        with pytest.raises(FabricQueryError) as exc_info:
            db._execute_with_retry(op, op_label="test", sql_for_hash="SELECT 1")

        assert exc_info.value.sqlstate == "42S02"
        assert len(calls) == 1  # no retry
        assert mock_connect.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_second_attempt_failure_raises_fabric_query_error(
        self, mock_connect: MagicMock
    ) -> None:
        """If the reconnect-and-retry attempt also fails with a
        connection error, surface FabricQueryError — don't loop forever."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.side_effect = [MagicMock(name="c1"), MagicMock(name="c2")]

        def op(conn: object) -> tuple[object, int]:
            raise pyodbc.Error("IMC06", "still broken")

        with pytest.raises(FabricQueryError) as exc_info:
            db._execute_with_retry(op, op_label="test", sql_for_hash="SELECT 1")

        assert exc_info.value.sqlstate == "IMC06"
        assert mock_connect.call_count == 2  # exactly one retry, no infinite loop


class TestIsConnectionError:
    """Pure-function tests of the SQLSTATE/message classifier. Each
    SQLSTATE family is asserted independently — no DB, no cursor mocks.
    The helper-seam test in TestExecuteWithRetry exercises the wiring
    that turns a True classification into a discard-and-retry.
    """

    def test_iso_08_class_is_connection_error(self) -> None:
        import pyodbc

        assert _is_connection_error(pyodbc.Error("08S01", "communication link failure"))
        assert _is_connection_error(pyodbc.Error("08001", "unable to connect"))
        assert _is_connection_error(pyodbc.Error("08003", "connection not open"))

    def test_imc_class_is_connection_error(self) -> None:
        """IMC* = Microsoft driver-side resiliency states. Once the
        driver sets IMC06, every subsequent cursor.execute returns
        IMC06 instantly with no round trip — discard is mandatory."""
        import pyodbc

        assert _is_connection_error(pyodbc.Error("IMC06", "client unrecoverable"))
        assert _is_connection_error(pyodbc.Error("IMC01", "recovery exhausted"))
        assert _is_connection_error(pyodbc.Error("IMC05", "server unrecoverable"))

    def test_hyt_class_is_connection_error(self) -> None:
        import pyodbc

        assert _is_connection_error(pyodbc.Error("HYT00", "Connection timeout expired"))
        assert _is_connection_error(pyodbc.Error("HYT01", "Login timeout"))

    def test_hy000_with_communication_link_failure_is_connection_error(self) -> None:
        """Generic SQLSTATE but the message fragment signals a real
        disconnect — the message-text fallback catches this."""
        import pyodbc

        assert _is_connection_error(
            pyodbc.Error("HY000", "[Microsoft]Communication link failure")
        )

    def test_hy000_with_tcp_provider_reset_is_connection_error(self) -> None:
        """Production-observed: WSAECONNRESET wrapped in HY000."""
        import pyodbc

        assert _is_connection_error(
            pyodbc.Error("HY000", "[TCP Provider] Error code 0x2746 (10054)")
        )

    def test_hy000_with_unrelated_message_is_not_connection_error(self) -> None:
        """Guard against the message-text fallback being too eager —
        a generic HY000 with no known disconnect phrase must NOT
        trigger reconnect."""
        import pyodbc

        assert not _is_connection_error(pyodbc.Error("HY000", "unrelated generic error"))

    def test_query_side_sqlstates_are_not_connection_errors(self) -> None:
        """42S02 (object not found), 22003 (overflow) etc. are
        legitimate query failures — must not be misclassified."""
        import pyodbc

        assert not _is_connection_error(pyodbc.Error("42S02", "Invalid object name"))
        assert not _is_connection_error(pyodbc.Error("22003", "Arithmetic overflow"))
        assert not _is_connection_error(pyodbc.Error("42000", "syntax error"))


class TestStaleConnectionRecovery:
    """End-to-end wiring smoke test through execute_query + the connection
    string content guard. SQLSTATE classification details live in
    TestIsConnectionError; retry policy in TestExecuteWithRetry. Keep
    this class small — it only verifies the seams compose, not the
    decisions inside each seam.

    Reproduces `.claude/bugfix/2026-05-22-stale-conn-imc06-not-retried/repro.md`.
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

    @patch("src.database.pyodbc.connect")
    def test_execute_query_reconnects_through_imc06_end_to_end(
        self, mock_connect: MagicMock
    ) -> None:
        """Production-realistic IMC06 chain — classifier + helper +
        execute_query must wire together to recover transparently."""
        import pyodbc

        db, _ = self._make_db()

        broken = MagicMock(name="broken")
        broken_cursor = MagicMock()
        broken_cursor.execute.side_effect = pyodbc.Error(
            "IMC06",
            "[IMC06] [Microsoft][ODBC Driver 18 for SQL Server]"
            "The connection is broken and recovery is not possible.",
        )
        broken.cursor.return_value = broken_cursor

        good = MagicMock(name="good")
        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = [(42,)]
        good.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken, good]

        _, rows = db.execute_query("SELECT 1")

        assert mock_connect.call_count == 2
        assert rows == [{"id": 42}]
        broken.close.assert_called()

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

    @patch("src.database.pyodbc.connect")
    def test_connection_string_enables_tcp_keepalive(
        self, mock_connect: MagicMock
    ) -> None:
        """Reproduces .claude/bugfix/2026-05-23-fabric-08s01-mid-query-tcp-drop:
        heavy Fabric-side compute (~20s) goes silent on the TCP wire;
        without an explicit KeepAlive < the intermediary's idle-drop
        threshold (observed ~20s), the connection is killed mid-query.
        Default driver KeepAlive=30s is too late."""
        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = cursor

        db.execute_query("SELECT 1")

        conn_string = mock_connect.call_args.args[0]
        assert "KeepAlive=" in conn_string, (
            "expected KeepAlive keyword to override the 30s default that "
            "leaves long-running queries vulnerable to intermediary "
            "TCP-idle drops"
        )
        assert "KeepAliveInterval=" in conn_string

    @patch("src.database.pyodbc.connect")
    def test_connection_string_carries_application_name(
        self, mock_connect: MagicMock
    ) -> None:
        """Application Name surfaces in Fabric's
        queryinsights.exec_requests_history.program_name so DBAs can
        filter requests originating from this MCP server without
        scraping container logs."""
        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = []
        mock_connect.return_value.cursor.return_value = cursor

        db.execute_query("SELECT 1")

        conn_string = mock_connect.call_args.args[0]
        # Accept either short-form `APP=` or long-form `Application Name=`
        assert ("APP=" in conn_string) or ("Application Name=" in conn_string), (
            "expected an APP / Application Name keyword so failures can be "
            "correlated to this MCP server in Fabric server-side telemetry"
        )


class TestObservability:
    """Per-attempt observability fields the operator needs to diagnose
    intermittent failures without cross-referencing wall-clock
    timestamps. Required fields: query_duration_ms, attempt, sql_hash."""

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
    def test_success_log_carries_query_duration_ms(
        self, mock_connect: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchall.return_value = [(1,)]
        mock_connect.return_value.cursor.return_value = cursor

        with caplog.at_level(logging.INFO, logger="fabric_mcp.database"):
            db.execute_query("SELECT 1")

        success_records = [
            r for r in caplog.records if "executed" in r.getMessage().lower()
        ]
        assert success_records, "expected at least one success log record"
        record = success_records[-1]
        assert hasattr(record, "query_duration_ms"), (
            "operator needs duration without reconstructing from timestamps"
        )
        assert isinstance(record.query_duration_ms, int)
        assert record.query_duration_ms >= 0

    @patch("src.database.pyodbc.connect")
    def test_retry_log_carries_attempt_and_duration(
        self, mock_connect: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The 'reconnecting and retrying' warning must show how long the
        first attempt took before failing — that's the smoking gun for
        the mid-query TCP-drop pattern."""
        import logging

        import pyodbc

        db, _ = self._make_db()
        broken = MagicMock(name="broken")
        broken_cursor = MagicMock()
        broken_cursor.execute.side_effect = pyodbc.Error("08S01", "Communication link failure")
        broken.cursor.return_value = broken_cursor

        good = MagicMock(name="good")
        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchall.return_value = []
        good.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken, good]

        with caplog.at_level(logging.WARNING, logger="fabric_mcp.database"):
            db.execute_query("SELECT * FROM big_table")

        retry_records = [
            r for r in caplog.records if "reconnecting" in r.getMessage().lower()
        ]
        assert retry_records, "expected a retry warning record"
        record = retry_records[-1]
        assert hasattr(record, "attempt"), "attempt field required"
        assert record.attempt == 0, (
            "retry warning fires when attempt 0 failed; field should reflect that"
        )
        assert hasattr(record, "query_duration_ms")
        assert hasattr(record, "sql_hash")
        assert isinstance(record.sql_hash, str)
        assert len(record.sql_hash) == 16  # sha256 first 16 hex chars

    @patch("src.database.pyodbc.connect")
    def test_final_error_log_carries_attempt_duration_sql_hash(
        self, mock_connect: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both attempts fail, the FabricQueryError surface needs
        attempt=1, duration, and sql_hash so the failure is fully
        characterised from a single log line."""
        import logging

        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        # First conn: raises on cursor.execute. Reconnect happens.
        # Second conn: also raises — this is the 'mid-query TCP drop' loop.
        first = MagicMock(name="first")
        first_cursor = MagicMock()
        first_cursor.execute.side_effect = pyodbc.Error("08S01", "Communication link failure")
        first.cursor.return_value = first_cursor

        second = MagicMock(name="second")
        second_cursor = MagicMock()
        second_cursor.execute.side_effect = pyodbc.Error("08S01", "Communication link failure")
        second.cursor.return_value = second_cursor

        mock_connect.side_effect = [first, second]

        with (
            caplog.at_level(logging.INFO, logger="fabric_mcp.database"),
            pytest.raises(FabricQueryError),
        ):
            db.execute_query("SELECT * FROM big_table")

        # The FabricQueryError raise path must emit a record that names
        # the failure mode and carries diagnostic fields.
        error_records = [
            r for r in caplog.records if r.levelname in {"ERROR", "WARNING"}
        ]
        assert error_records, "expected an error/warning record on final failure"
        # The LAST warning/error record is the one that announces the
        # exhausted retry; check it carries the three required fields.
        record = error_records[-1]
        assert hasattr(record, "attempt")
        assert record.attempt == 1, "second attempt is attempt=1"
        assert hasattr(record, "query_duration_ms")
        assert hasattr(record, "sql_hash")

    def test_sql_hash_is_stable_and_short(self) -> None:
        """Internal: hash function used in logs must be stable for the
        same SQL and short enough for log readability."""
        from src.database import _sql_hash

        sql = "SELECT * FROM gold.vw_Sch1X_EntityUSD WHERE FiscalMonth = 4"
        h1 = _sql_hash(sql)
        h2 = _sql_hash(sql)
        assert h1 == h2
        assert isinstance(h1, str)
        assert len(h1) == 16
        # Hex characters only — readable, paste-able.
        int(h1, 16)  # no exception → valid hex
