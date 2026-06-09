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
        mock_cursor.fetchmany.side_effect = [[(1, "Alice"), (2, "Bob")], []]
        mock_connect.return_value.cursor.return_value = mock_cursor

        columns, rows = db.execute_query("SELECT id, name FROM test")

        assert len(columns) == 2
        assert columns[0].name == "id"
        assert columns[1].nullable is True
        assert len(rows) == 2
        assert rows[0] == {"id": 1, "name": "Alice"}

    @patch("src.database.pyodbc.connect")
    def test_execute_query_uses_batched_fetchmany_not_fetchall(
        self, mock_connect: MagicMock
    ) -> None:
        """Per pyodbc docs, cursor.fetchall() does NOT honor cursor.arraysize —
        only fetchmany(N) gets batched SQLFetchScroll calls from the driver.
        Lock in the batched path so a refactor cannot silently regress to
        per-row fetches (the dominant Fabric round-trip cost)."""
        from src.database import _FETCH_BATCH_SIZE

        db, _ = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        # Two chunks then sentinel — proves the loop drives fetchmany repeatedly
        # until it returns empty, not a single fetchall() call.
        mock_cursor.fetchmany.side_effect = [[(1,), (2,)], [(3,)], []]
        mock_connect.return_value.cursor.return_value = mock_cursor

        _, rows = db.execute_query("SELECT id FROM t")

        assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert mock_cursor.arraysize == _FETCH_BATCH_SIZE, (
            "cursor.arraysize must be set so any later fetchmany() default-size "
            "call also batches"
        )
        assert mock_cursor.fetchmany.call_count == 3, (
            "expected 3 fetchmany calls (chunk, chunk, empty terminator)"
        )
        mock_cursor.fetchall.assert_not_called()
        # Every fetchmany call must request the documented batch size.
        for call in mock_cursor.fetchmany.call_args_list:
            assert call.args == (_FETCH_BATCH_SIZE,)

    @patch("src.database.pyodbc.connect")
    def test_execute_query_bounds_fetch_to_max_rows_plus_one(
        self, mock_connect: MagicMock
    ) -> None:
        """With max_rows set, execute_query must stop fetching at cap+1 rows
        (cap+1 so the caller can still detect truncation) instead of draining
        the whole result set into memory (P8)."""
        db, _ = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        # First chunk already holds far more than the cap; a draining impl would
        # also call fetchmany again for the terminator.
        mock_cursor.fetchmany.side_effect = [[(i,) for i in range(10_000)], []]
        mock_connect.return_value.cursor.return_value = mock_cursor

        _, rows = db.execute_query("SELECT id FROM huge", max_rows=2)

        assert len(rows) == 3, "must materialise at most cap+1 rows, not the whole set"
        assert mock_cursor.fetchmany.call_count == 1, (
            "must stop fetching once cap+1 is reached, not drain to the terminator"
        )

    @patch("src.database.pyodbc.connect")
    def test_execute_query_without_max_rows_still_drains(
        self, mock_connect: MagicMock
    ) -> None:
        """Regression: with no max_rows (e.g. internal schema queries), the full
        result set is still returned."""
        db, _ = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchmany.side_effect = [[(1,), (2,)], [(3,)], []]
        mock_connect.return_value.cursor.return_value = mock_cursor

        _, rows = db.execute_query("SELECT id FROM t")

        assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]

    @patch("src.database.pyodbc.connect")
    def test_execute_query_empty_result(self, mock_connect: MagicMock) -> None:
        db, _ = self._make_db()

        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchmany.return_value = []
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
    def test_error_message_redacts_server_and_database_identifiers(
        self, mock_connect: MagicMock
    ) -> None:
        """ODBC diagnostics embed the Fabric hostname + database name. The
        on-wire FabricQueryError.message must not leak them across the trust
        boundary; benign query-side text is preserved (P9)."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()  # server=test.datawarehouse.fabric.microsoft.com, db=gold_warehouse
        raw = (
            "[Microsoft][ODBC Driver 18 for SQL Server]Cannot open database "
            "'gold_warehouse' requested by the login on server "
            "'test.datawarehouse.fabric.microsoft.com'."
        )
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "28000", raw
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT 1")

        msg = exc_info.value.message
        assert "test.datawarehouse.fabric.microsoft.com" not in msg, (
            f"server hostname must be redacted from the on-wire message; got {msg}"
        )
        assert "gold_warehouse" not in msg, "database name must be redacted"
        # The SQLSTATE is still surfaced for the caller to act on.
        assert exc_info.value.sqlstate == "28000"

    @patch("src.database.pyodbc.connect")
    def test_error_message_preserves_query_side_text(
        self, mock_connect: MagicMock
    ) -> None:
        """Redaction must not strip useful query-side diagnostics that help the
        caller self-correct (these contain no server/db identifiers)."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "42S02", "Invalid object name 'foo'"
        )

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_query("SELECT * FROM foo")
        assert "Invalid object name" in exc_info.value.message

    @patch("src.database.pyodbc.connect")
    def test_full_error_text_logged_server_side(
        self, mock_connect: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raw error (incl. identifiers) must still be available server-side
        for diagnosis — log server-side, scrub client-side."""
        import logging

        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        raw = "Cannot open database 'gold_warehouse' on test.datawarehouse.fabric.microsoft.com."
        mock_connect.return_value.cursor.return_value.execute.side_effect = pyodbc.Error(
            "28000", raw
        )

        with (
            caplog.at_level(logging.ERROR, logger="fabric_mcp.database"),
            pytest.raises(FabricQueryError),
        ):
            db.execute_query("SELECT 1")

        logged = " ".join(
            str(getattr(r, "error_detail", "")) for r in caplog.records
        )
        assert "gold_warehouse" in logged, (
            "full raw error must be retained server-side via the error_detail field"
        )

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
    def test_execute_writes_empty_list_is_noop(self, mock_connect: MagicMock) -> None:
        """Empty input must short-circuit before acquiring the connection —
        the batch path must not pay handshake / cursor cost for zero work."""
        db, _ = self._make_db()
        assert db.execute_writes([]) == []
        mock_connect.assert_not_called()

    @patch("src.database.pyodbc.connect")
    def test_execute_writes_batch_uses_single_lock_and_cursor(
        self, mock_connect: MagicMock
    ) -> None:
        """The whole point of execute_writes: amortise the lock + cursor
        + retry-stack overhead across N statements. Locking-in: one
        cursor() call regardless of N, single connect() call."""
        db, _ = self._make_db()

        cursor = MagicMock()
        # rowcount is read after each execute; the same value is fine here
        # since we're checking call topology, not per-statement counts.
        cursor.rowcount = 1
        mock_connect.return_value.cursor.return_value = cursor

        sqls = [
            "INSERT INTO t (id) VALUES (1)",
            "INSERT INTO t (id) VALUES (2)",
            "INSERT INTO t (id) VALUES (3)",
        ]
        results = db.execute_writes(sqls)

        assert results == [1, 1, 1]
        assert mock_connect.call_count == 1, "single pyodbc.connect across batch"
        assert mock_connect.return_value.cursor.call_count == 1, (
            "single cursor allocated across batch — per-statement cursor() "
            "would re-pay SQLAllocHandle for each row, defeating the point"
        )
        assert cursor.execute.call_count == 3
        # Each statement was driven through the same cursor in order.
        executed_sqls = [c.args[0] for c in cursor.execute.call_args_list]
        assert executed_sqls == sqls

    @patch("src.database.pyodbc.connect")
    def test_execute_writes_per_statement_error_does_not_abort(
        self, mock_connect: MagicMock
    ) -> None:
        """A query-side error on statement N must return a FabricQueryError
        in slot N and continue executing statements N+1..end."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()

        cursor = MagicMock()
        # statement 0 succeeds (rowcount=1), statement 1 raises,
        # statement 2 succeeds (rowcount=2).
        cursor.rowcount = 1
        cursor.execute.side_effect = [
            None,
            pyodbc.Error("23000", "Violation of PRIMARY KEY constraint"),
            None,
        ]
        mock_connect.return_value.cursor.return_value = cursor

        results = db.execute_writes(
            [
                "INSERT INTO t VALUES (1)",
                "INSERT INTO t VALUES (1)",  # duplicate → constraint error
                "INSERT INTO t VALUES (2)",
            ]
        )

        assert len(results) == 3
        assert results[0] == 1
        assert isinstance(results[1], FabricQueryError)
        assert results[1].sqlstate == "23000"
        assert "PRIMARY KEY" in results[1].message
        assert results[2] == 1
        # All three were attempted on the same cursor — no early abort.
        assert cursor.execute.call_count == 3

    @patch("src.database.pyodbc.connect")
    def test_execute_writes_connection_error_mid_batch_does_not_retry_failed_stmt(
        self, mock_connect: MagicMock
    ) -> None:
        """If a connection-class error fires mid-batch, the failing statement
        must NOT be re-executed (its server-side commit state is unknown — a
        retry would double-apply). It is flagged WRITE_STATE_UNKNOWN, the
        connection is rebuilt, and *subsequent* statements continue on the new
        connection. Statements before the failure keep their successes.

        Updated from the prior retry-the-failed-statement contract, which
        silently double-applied non-idempotent writes (review finding P2)."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()

        # First connection: statement 0 OK; statement 1 raises 08S01.
        broken_conn = MagicMock(name="broken")
        broken_cursor = MagicMock()
        broken_cursor.rowcount = 1
        broken_cursor.execute.side_effect = [
            None,
            pyodbc.Error("08S01", "Communication link failure"),
        ]
        broken_conn.cursor.return_value = broken_cursor

        # Second connection: only statement 2 runs here (statement 1 is NOT
        # retried).
        good_conn = MagicMock(name="good")
        good_cursor = MagicMock()
        good_cursor.rowcount = 1
        good_cursor.execute.side_effect = [None]
        good_conn.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken_conn, good_conn]

        results = db.execute_writes(
            [
                "INSERT INTO t VALUES (1)",
                "INSERT INTO t VALUES (2)",
                "INSERT INTO t VALUES (3)",
            ]
        )

        assert results[0] == 1
        assert isinstance(results[1], FabricQueryError), (
            "the dropped statement must be flagged, not silently retried"
        )
        assert results[1].code == "WRITE_STATE_UNKNOWN"
        assert results[1].sqlstate == "08S01"
        assert results[2] == 1
        assert mock_connect.call_count == 2, "rebuild the connection once for stmt 2"
        broken_conn.close.assert_called()
        # Statement 0 + the dropped statement 1 ran on broken cursor — and
        # statement 1 was NOT re-executed.
        assert broken_cursor.execute.call_count == 2
        # Only statement 2 ran on the new cursor (no retry of statement 1).
        assert good_cursor.execute.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_execute_writes_connection_error_flags_unknown_state_no_retry(
        self, mock_connect: MagicMock
    ) -> None:
        """A single batched write that drops mid-flight is flagged
        WRITE_STATE_UNKNOWN and is NOT re-executed (no second connection, no
        second execute)."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()

        conn = MagicMock(name="conn")
        cursor = MagicMock()
        cursor.execute.side_effect = pyodbc.Error("08S01", "link down")
        conn.cursor.return_value = cursor
        mock_connect.return_value = conn

        results = db.execute_writes(["INSERT INTO t VALUES (1)"])

        assert len(results) == 1
        assert isinstance(results[0], FabricQueryError)
        assert results[0].code == "WRITE_STATE_UNKNOWN"
        assert results[0].sqlstate == "08S01"
        assert cursor.execute.call_count == 1, "the write must not be re-executed"
        assert mock_connect.call_count == 1, "no reconnect needed — nothing left to run"


class TestWriteRetrySafety:
    """Writes must not be auto-retried on a connection-class error: under
    autocommit the server may have committed before the drop/timeout, so a
    retry double-applies a non-idempotent write (review findings P1/P2/P10).
    Reads stay retryable (idempotent)."""

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
    def test_execute_write_does_not_retry_on_connection_drop(
        self, mock_connect: MagicMock
    ) -> None:
        """P1: an 08S01 mid-flight drop on a write must NOT re-execute the
        statement. Surface WRITE_STATE_UNKNOWN so the caller can reconcile."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.rowcount = 1
        cursor.execute.side_effect = pyodbc.Error("08S01", "Communication link failure")
        mock_connect.return_value.cursor.return_value = cursor

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_write("INSERT INTO raw.Fact_Sch1X (x) VALUES (1)")

        assert exc_info.value.code == "WRITE_STATE_UNKNOWN"
        assert exc_info.value.sqlstate == "08S01"
        assert cursor.execute.call_count == 1, "write must run exactly once (no retry)"

    @patch("src.database.pyodbc.connect")
    def test_execute_write_does_not_retry_on_timeout(
        self, mock_connect: MagicMock
    ) -> None:
        """P1/P10: a HYT00 timeout gives no signal about server-side commit
        state, so the write must NOT be retried."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        cursor = MagicMock()
        cursor.rowcount = 1
        cursor.execute.side_effect = pyodbc.Error("HYT00", "Query timeout expired")
        mock_connect.return_value.cursor.return_value = cursor

        with pytest.raises(FabricQueryError) as exc_info:
            db.execute_write("UPDATE raw.Fact_Sch1X SET x=1")

        assert exc_info.value.code == "WRITE_STATE_UNKNOWN"
        assert cursor.execute.call_count == 1

    @patch("src.database.pyodbc.connect")
    def test_execute_write_discards_connection_on_drop(
        self, mock_connect: MagicMock
    ) -> None:
        """The dropped connection must be discarded so the next call opens a
        fresh one (don't keep reusing a dead socket)."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        conn = MagicMock(name="conn")
        cursor = MagicMock()
        cursor.execute.side_effect = pyodbc.Error("08S01", "link failure")
        conn.cursor.return_value = cursor
        mock_connect.return_value = conn

        with pytest.raises(FabricQueryError):
            db.execute_write("INSERT INTO t VALUES (1)")

        conn.close.assert_called()

    @patch("src.database.pyodbc.connect")
    def test_query_still_retries_on_connection_drop(
        self, mock_connect: MagicMock
    ) -> None:
        """Regression guard: reads are idempotent and MUST keep the
        reconnect-and-retry behavior."""
        import pyodbc

        db, _ = self._make_db()

        broken = MagicMock(name="broken")
        broken_cursor = MagicMock()
        broken_cursor.execute.side_effect = pyodbc.Error("08S01", "link failure")
        broken.cursor.return_value = broken_cursor

        good = MagicMock(name="good")
        good_cursor = MagicMock()
        good_cursor.description = [("id", int, None, None, None, None, False)]
        good_cursor.fetchmany.side_effect = [[(1,)], []]
        good.cursor.return_value = good_cursor

        mock_connect.side_effect = [broken, good]

        _, rows = db.execute_query("SELECT 1")

        assert rows == [{"id": 1}]
        assert mock_connect.call_count == 2, "reads must still reconnect-and-retry"

    @patch("src.database.pyodbc.connect")
    def test_write_resets_residual_query_timeout(
        self, mock_connect: MagicMock
    ) -> None:
        """P10: a write's effective timeout must be deterministic, not inherited
        from the last query's residual conn.timeout on the shared connection."""
        db, _ = self._make_db()
        conn = mock_connect.return_value
        cursor = MagicMock()
        cursor.description = [("id", int, None, None, None, None, False)]
        cursor.fetchmany.return_value = []
        cursor.rowcount = 1
        conn.cursor.return_value = cursor

        db.execute_query("SELECT 1", timeout=30)
        assert conn.timeout == 30, "query sets its own timeout"

        db.execute_write("UPDATE t SET c=1")
        assert conn.timeout == 0, (
            "write must set a deterministic timeout, not inherit the query's 30s"
        )

    @patch("src.database.pyodbc.connect")
    def test_execute_with_retry_no_retry_flag_surfaces_unknown_state(
        self, mock_connect: MagicMock
    ) -> None:
        """Seam test: with retry_on_connection_error=False, a connection-class
        error runs the op exactly once and raises WRITE_STATE_UNKNOWN."""
        import pyodbc

        from src.database import FabricQueryError

        db, _ = self._make_db()
        mock_connect.return_value = MagicMock(name="conn")

        calls: list[object] = []

        def op(conn: object) -> tuple[object, int]:
            calls.append(conn)
            raise pyodbc.Error("08S01", "link failure")

        with pytest.raises(FabricQueryError) as exc_info:
            db._execute_with_retry(
                op,
                op_label="write",
                sql_for_hash="INSERT INTO t VALUES (1)",
                retry_on_connection_error=False,
            )

        assert exc_info.value.code == "WRITE_STATE_UNKNOWN"
        assert len(calls) == 1, "op must run exactly once (no retry)"

    @patch("src.database.pyodbc.connect")
    def test_connection_uses_token_auth(self, mock_connect: MagicMock) -> None:
        db, mock_auth = self._make_db()
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", int, None, None, None, None, False)]
        mock_cursor.fetchmany.return_value = []
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
        mock_cursor.fetchmany.return_value = []
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
        cursor.fetchmany.return_value = []
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
        good_cursor.fetchmany.side_effect = [[(1,)], []]
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
        good_cursor.fetchmany.return_value = []

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
        good_cursor.fetchmany.side_effect = [[(42,)], []]
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
        cursor.fetchmany.return_value = []
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
        cursor.fetchmany.return_value = []
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
        cursor.fetchmany.return_value = []
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
        cursor.fetchmany.side_effect = [[(1,)], []]
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
        good_cursor.fetchmany.return_value = []
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


class TestConnectionPoolingDisabled:
    """Reproduces .claude/bugfix/2026-06-09-pyodbc-pooling-defeats-reconnect.

    The application caches a single connection and recovers from a dead one
    by `_discard_connection()` (`close()`) + reopening. That recovery only
    works if ODBC connection pooling is OFF: with pyodbc's default
    `pooling=True`, `close()` returns the dead connection to the unixODBC
    pool and the next `pyodbc.connect()` with the identical connection string
    draws the SAME dead handle back out — so the existing 08S01 reconnect is
    silently defeated and every query fails until the process restarts.

    pyodbc docs: `pooling` defaults to True and can only be changed before the
    first connection (it configures the shared HENV). Importing `src.database`
    must therefore have already turned it off.
    """

    def test_module_import_disables_odbc_pooling(self) -> None:
        import pyodbc

        import src.database  # noqa: F401  (import for its import-time side effect)

        assert pyodbc.pooling is False, (
            "src.database must set pyodbc.pooling = False at import (before any "
            "connect) so _discard_connection() truly closes the dead connection "
            "and the 08S01 reconnect actually rebuilds it instead of pulling the "
            "same dead handle back out of the ODBC pool"
        )
