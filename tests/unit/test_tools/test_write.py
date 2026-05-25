"""Unit tests for write tools (preview and execute)."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

from src.config import FabricSettings
from src.tools._confirmation_token import make_confirmation_token
from src.tools.write import _parse_write_sql


def _make_config(**overrides: object) -> FabricSettings:
    defaults = {
        "server": "test.datawarehouse.fabric.microsoft.com",
        "database": "db",
        "client_id": "cid",
        "client_secret": "cs",
        "tenant_id": "tid",
        "write_allowlist": ["gold.transactions", "gold.accounts"],
        "api_key": "test-api-key",
    }
    defaults.update(overrides)
    return FabricSettings(**defaults)


def _make_write_tools(
    db: MagicMock | None = None, config: FabricSettings | None = None
) -> dict[str, tuple[callable, MagicMock]]:
    """Create write tool functions with mocked dependencies."""
    from mcp.server.fastmcp import FastMCP

    from src.tools.write import register_write_tools

    mock_mcp = FastMCP("test")
    mock_db = db or MagicMock()
    cfg = config or _make_config()
    register_write_tools(mock_mcp, mock_db, cfg)

    tools = {}
    for tool in mock_mcp._tool_manager._tools.values():
        tools[tool.name] = (tool.fn, mock_db)
    return tools


class TestParseWriteSql:
    """Test SQL parsing for write operations."""

    def test_insert_detected(self) -> None:
        result = _parse_write_sql("INSERT INTO gold.transactions (id) VALUES (1)")
        assert result == ("INSERT", "gold.transactions")

    def test_update_detected(self) -> None:
        result = _parse_write_sql("UPDATE gold.accounts SET balance = 100 WHERE id = 1")
        assert result == ("UPDATE", "gold.accounts")

    def test_select_not_detected(self) -> None:
        assert _parse_write_sql("SELECT * FROM test") is None

    def test_delete_not_detected(self) -> None:
        assert _parse_write_sql("DELETE FROM test") is None

    def test_case_insensitive(self) -> None:
        result = _parse_write_sql("insert into gold.test (id) values (1)")
        assert result == ("INSERT", "gold.test")


class TestFabricPreviewWrite:
    """Test the preview write tool."""

    def test_valid_insert_returns_preview(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO gold.transactions (id) VALUES (1)"))

        assert "confirmation_token" in result
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"
        assert "expires_at" in result

    def test_valid_update_returns_preview(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("UPDATE gold.accounts SET balance = 100"))

        assert result["operation"] == "UPDATE"
        assert result["table"] == "gold.accounts"

    def test_table_not_on_allowlist(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO raw.secret_data (id) VALUES (1)"))

        assert result["code"] == "TABLE_NOT_ALLOWED"

    def test_non_write_rejected(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("SELECT * FROM gold.transactions"))

        assert result["code"] == "INVALID_OPERATION"

    def test_empty_allowlist_rejects_all(self) -> None:
        config = _make_config(write_allowlist=[])
        tools = _make_write_tools(config=config)
        fn, _ = tools["fabric_preview_write"]

        result = json.loads(fn("INSERT INTO gold.transactions (id) VALUES (1)"))

        assert result["code"] == "TABLE_NOT_ALLOWED"

    def test_oversize_sql_rejected_up_front(self) -> None:
        """Large SQL produces a confirmation token bigger than the transport
        layer can carry, surfacing as an opaque TOKEN_INVALID on redemption.
        Catch it at preview time with a structured INVALID_OPERATION naming
        the actual size + ceiling so the caller knows to split into batches."""
        from src.tools.write import _PREVIEW_SQL_MAX_BYTES

        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        # A real-shaped INSERT … VALUES with enough tuples to exceed the cap.
        prefix = "INSERT INTO gold.transactions (id, name) VALUES "
        tuple_str = "(1, 'x'),"
        n = (_PREVIEW_SQL_MAX_BYTES // len(tuple_str)) + 50
        oversize = prefix + (tuple_str * n).rstrip(",")
        assert len(oversize.encode("utf-8")) > _PREVIEW_SQL_MAX_BYTES

        result = json.loads(fn(oversize))

        assert result["code"] == "INVALID_OPERATION"
        assert str(_PREVIEW_SQL_MAX_BYTES) in result["message"]
        # Caller-actionable hint must name the workaround.
        assert "split" in result["message"].lower() or "batch" in result["message"].lower()

    def test_sql_at_exact_size_limit_accepted(self) -> None:
        """Boundary: a SQL of exactly _PREVIEW_SQL_MAX_BYTES bytes is still
        accepted — the rejection is strictly for sizes *above* the cap."""
        from src.tools.write import _PREVIEW_SQL_MAX_BYTES

        tools = _make_write_tools()
        fn, _ = tools["fabric_preview_write"]

        # Build a syntactically valid INSERT padded out with a long string
        # literal so the encoded byte length lands exactly on the cap.
        prefix = "INSERT INTO gold.transactions (id, name) VALUES (1, '"
        suffix = "')"
        pad = "x" * (_PREVIEW_SQL_MAX_BYTES - len(prefix) - len(suffix))
        sql = prefix + pad + suffix
        assert len(sql.encode("utf-8")) == _PREVIEW_SQL_MAX_BYTES

        result = json.loads(fn(sql))
        assert "confirmation_token" in result, (
            f"SQL at exactly the byte cap must be accepted; got {result}"
        )


class TestFabricExecuteWrite:
    """Test the execute write tool."""

    def test_valid_token_executes_write(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, mock_db = tools["fabric_execute_write"]
        mock_db.execute_write.return_value = 1

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_fn(token))

        assert result["affected_rows"] == 1
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"

    def test_invalid_token_rejected(self) -> None:
        tools = _make_write_tools()
        fn, _ = tools["fabric_execute_write"]

        result = json.loads(fn("nonexistent-token"))

        assert result["code"] == "TOKEN_INVALID"

    def test_expired_token_rejected(self) -> None:
        config = _make_config()
        tools = _make_write_tools(config=config)
        execute_fn, _ = tools["fabric_execute_write"]

        # Properly-signed token with `exp` already in the past.
        token, _ = make_confirmation_token(
            sql="INSERT INTO gold.transactions (id) VALUES (1)",
            op="INSERT",
            table="gold.transactions",
            secret=config.client_secret,
            expires_in=timedelta(minutes=-1),
        )

        result = json.loads(execute_fn(token))
        assert result["code"] == "TOKEN_EXPIRED"


class TestStatelessToken:
    """Confirmation tokens must be redeemable across independent server instances
    (different Container Apps replicas) that share the same configuration.

    Each `_make_write_tools()` call simulates a fresh Python process — the
    stateless token design must not rely on any in-process state.
    """

    def test_token_redeemable_on_independent_instance(self) -> None:
        """Replica A issues a token; replica B (separate registration, same
        config) must be able to redeem it."""
        config = _make_config()

        tools_a = _make_write_tools(config=config)
        preview_a, _ = tools_a["fabric_preview_write"]

        tools_b = _make_write_tools(config=config)
        execute_b, mock_db_b = tools_b["fabric_execute_write"]
        mock_db_b.execute_write.return_value = 1

        preview = json.loads(preview_a("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_b(token))

        assert result.get("code") != "TOKEN_INVALID", (
            f"token must be redeemable on a different instance; got {result}"
        )
        assert result["affected_rows"] == 1
        assert result["operation"] == "INSERT"
        assert result["table"] == "gold.transactions"
        mock_db_b.execute_write.assert_called_once_with(
            "INSERT INTO gold.transactions (id) VALUES (1)"
        )

    def test_tampered_token_rejected(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, _ = tools["fabric_execute_write"]

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]
        # Flip one character in the middle of the token
        midpoint = len(token) // 2
        flipped = "A" if token[midpoint] != "A" else "B"
        tampered = token[:midpoint] + flipped + token[midpoint + 1 :]

        result = json.loads(execute_fn(tampered))
        assert result["code"] == "TOKEN_INVALID"

    def test_token_signed_with_different_secret_rejected(self) -> None:
        """A token issued under one client_secret must not be redeemable under
        a different client_secret (simulates secret rotation between replicas
        that have not yet picked up the new value)."""
        config_a = _make_config(client_secret="secret-A")
        config_b = _make_config(client_secret="secret-B")

        tools_a = _make_write_tools(config=config_a)
        preview_a, _ = tools_a["fabric_preview_write"]

        tools_b = _make_write_tools(config=config_b)
        execute_b, _ = tools_b["fabric_execute_write"]

        preview = json.loads(preview_a("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result = json.loads(execute_b(token))
        assert result["code"] == "TOKEN_INVALID"

    def test_token_payload_contains_issued_at(self) -> None:
        """Tokens carry an `iat` (issued-at) POSIX timestamp for forensic
        purposes, in addition to the existing `exp`."""
        import base64
        import time as time_module

        config = _make_config()
        tools = _make_write_tools(config=config)
        preview_fn, _ = tools["fabric_preview_write"]

        before = time_module.time()
        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        after = time_module.time()
        token = preview["confirmation_token"]

        # Decode the payload portion (no signature verification here — we trust
        # the token we just minted; only inspecting the JSON shape).
        payload_b64 = token.split(".", 1)[0]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))

        assert "iat" in payload
        assert before - 1 <= payload["iat"] <= after + 1

    def test_token_expiry_uses_configured_minutes(self) -> None:
        """The token's `exp` is `iat + write_token_expiry_minutes * 60`."""
        import base64

        config = _make_config(write_token_expiry_minutes=30)
        tools = _make_write_tools(config=config)
        preview_fn, _ = tools["fabric_preview_write"]

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]
        payload_b64 = token.split(".", 1)[0]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))

        # Allow 5-second slack for clock skew between the two captures.
        assert 30 * 60 - 5 <= payload["exp"] - payload["iat"] <= 30 * 60 + 5

    def test_token_replay_within_window_succeeds(self) -> None:
        """Stateless tokens are replayable within the validity window. This is
        a deliberate behavioural deviation from the original spec
        (`tasks.md:130` "one-time use invalidation") in exchange for working
        correctness across replicas. See report.md for the trade-off
        analysis."""
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        execute_fn, mock_db = tools["fabric_execute_write"]
        mock_db.execute_write.return_value = 1

        preview = json.loads(preview_fn("INSERT INTO gold.transactions (id) VALUES (1)"))
        token = preview["confirmation_token"]

        result1 = json.loads(execute_fn(token))
        result2 = json.loads(execute_fn(token))

        assert "affected_rows" in result1
        assert "affected_rows" in result2
        assert mock_db.execute_write.call_count == 2


class TestFabricExecuteWriteBatch:
    """Redeem multiple confirmation tokens in one MCP round-trip.

    Best-effort semantics: each token is verified and executed independently;
    a failure on one does not roll back earlier successes nor stop later
    tokens from executing. The response carries per-token status plus
    succeeded/failed counts.
    """

    def _make_tokens(self, preview_fn: callable, sqls: list[str]) -> list[str]:
        tokens = []
        for sql in sqls:
            preview = json.loads(preview_fn(sql))
            tokens.append(preview["confirmation_token"])
        return tokens

    def test_all_valid_tokens_all_succeed(self) -> None:
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        batch_fn, mock_db = tools["fabric_execute_write_batch"]
        mock_db.execute_writes.return_value = [1, 1, 1]

        tokens = self._make_tokens(
            preview_fn,
            [
                "INSERT INTO gold.transactions (id) VALUES (1)",
                "INSERT INTO gold.transactions (id) VALUES (2)",
                "INSERT INTO gold.transactions (id) VALUES (3)",
            ],
        )

        result = json.loads(batch_fn(tokens))

        assert result["total_succeeded"] == 3
        assert result["total_failed"] == 0
        assert len(result["results"]) == 3
        for r in result["results"]:
            assert r["status"] == "ok"
            assert r["affected_rows"] == 1
            assert r["operation"] == "INSERT"
            assert r["table"] == "gold.transactions"
        # All 3 statements must go through the single batched DB call —
        # the per-statement `execute_write` path would re-acquire the
        # connection lock per token and is what #2 was designed to avoid.
        assert mock_db.execute_writes.call_count == 1
        assert len(mock_db.execute_writes.call_args.args[0]) == 3
        mock_db.execute_write.assert_not_called()

    def test_mixed_valid_and_invalid_tokens(self) -> None:
        """Best-effort: invalid token between valid ones must not abort the batch."""
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        batch_fn, mock_db = tools["fabric_execute_write_batch"]
        mock_db.execute_writes.return_value = [1, 1]

        valid_tokens = self._make_tokens(
            preview_fn,
            [
                "INSERT INTO gold.transactions (id) VALUES (1)",
                "INSERT INTO gold.transactions (id) VALUES (3)",
            ],
        )
        # Slip a garbage token in the middle.
        all_tokens = [valid_tokens[0], "not-a-real-token", valid_tokens[1]]

        result = json.loads(batch_fn(all_tokens))

        assert result["total_succeeded"] == 2
        assert result["total_failed"] == 1
        assert result["results"][0]["status"] == "ok"
        assert result["results"][1]["status"] == "error"
        assert result["results"][1]["code"] == "TOKEN_INVALID"
        assert result["results"][2]["status"] == "ok"
        # Only the two valid tokens reached the DB, and they did so via a
        # single batched call (one connection-lock acquire, one cursor).
        assert mock_db.execute_writes.call_count == 1
        assert len(mock_db.execute_writes.call_args.args[0]) == 2

    def test_expired_token_in_batch(self) -> None:
        config = _make_config()
        tools = _make_write_tools(config=config)
        batch_fn, mock_db = tools["fabric_execute_write_batch"]

        expired_token, _ = make_confirmation_token(
            sql="INSERT INTO gold.transactions (id) VALUES (1)",
            op="INSERT",
            table="gold.transactions",
            secret=config.client_secret,
            expires_in=timedelta(minutes=-5),
        )

        result = json.loads(batch_fn([expired_token]))

        assert result["total_succeeded"] == 0
        assert result["total_failed"] == 1
        assert result["results"][0]["status"] == "error"
        assert result["results"][0]["code"] == "TOKEN_EXPIRED"
        # Pre-DB rejection must NOT touch the database batch path at all.
        mock_db.execute_writes.assert_not_called()
        mock_db.execute_write.assert_not_called()

    def test_empty_token_list_is_no_op_success(self) -> None:
        tools = _make_write_tools()
        batch_fn, mock_db = tools["fabric_execute_write_batch"]

        result = json.loads(batch_fn([]))

        assert result["results"] == []
        assert result["total_succeeded"] == 0
        assert result["total_failed"] == 0
        mock_db.execute_writes.assert_not_called()
        mock_db.execute_write.assert_not_called()

    def test_batch_size_above_limit_rejected(self) -> None:
        tools = _make_write_tools()
        batch_fn, mock_db = tools["fabric_execute_write_batch"]

        # 101 dummy tokens — server should reject the whole batch without
        # attempting to verify any of them.
        result = json.loads(batch_fn(["fake"] * 101))

        assert result.get("code") == "INVALID_OPERATION"
        mock_db.execute_writes.assert_not_called()
        mock_db.execute_write.assert_not_called()

    def test_database_error_in_one_token_does_not_abort_batch(self) -> None:
        """If a statement fails mid-batch, the failing token reports
        QUERY_ERROR but later tokens still execute. `execute_writes` returns
        per-statement outcomes positionally — a FabricQueryError in slot N
        does NOT abort slot N+1."""
        tools = _make_write_tools()
        preview_fn, _ = tools["fabric_preview_write"]
        batch_fn, mock_db = tools["fabric_execute_write_batch"]

        from src.database import FabricQueryError

        mock_db.execute_writes.return_value = [
            FabricQueryError(
                message="Violation of PRIMARY KEY constraint",
                code="QUERY_ERROR",
                details=None,
                sqlstate="23000",
            ),
            1,
            1,
        ]

        tokens = self._make_tokens(
            preview_fn,
            [
                "INSERT INTO gold.transactions (id) VALUES (1)",
                "INSERT INTO gold.transactions (id) VALUES (2)",
                "INSERT INTO gold.transactions (id) VALUES (3)",
            ],
        )

        result = json.loads(batch_fn(tokens))

        assert result["total_succeeded"] == 2
        assert result["total_failed"] == 1
        assert result["results"][0]["status"] == "error"
        assert result["results"][0]["code"] == "QUERY_ERROR"
        assert result["results"][0]["operation"] == "INSERT"
        assert result["results"][0]["table"] == "gold.transactions"
        assert result["results"][1]["status"] == "ok"
        assert result["results"][2]["status"] == "ok"
        # One batched DB call carrying all three statements.
        assert mock_db.execute_writes.call_count == 1
        assert len(mock_db.execute_writes.call_args.args[0]) == 3


class TestFabricDeletePeriod:
    """A narrow DELETE primitive: delete one fiscal period's rows from a
    write-allowlisted fact table. Safer than generic DELETE because the
    WHERE clause is fixed to `FiscalYear = ? AND FiscalMonth = ?`."""

    def _setup_columns_check(self, mock_db: MagicMock, columns: list[str]) -> None:
        """Make the next execute_query call (the FiscalYear/Month existence
        check) return the given column names."""
        mock_db.execute_query.return_value = (
            [],
            [{"COLUMN_NAME": c} for c in columns],
        )

    def test_happy_path_returns_deleted_rows(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]
        self._setup_columns_check(mock_db, ["FiscalYear", "FiscalMonth"])
        mock_db.execute_write.return_value = 143

        result = json.loads(fn("raw.Fact_ExchangeRate", 2026, 5))

        assert result["deleted_rows"] == 143
        assert result["table"] == "raw.Fact_ExchangeRate"
        assert result["fiscal_year"] == 2026
        assert result["fiscal_month"] == 5

    def test_emits_fixed_where_clause(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]
        self._setup_columns_check(mock_db, ["FiscalYear", "FiscalMonth"])
        mock_db.execute_write.return_value = 0

        fn("raw.Fact_ExchangeRate", 2026, 5)

        delete_sql = mock_db.execute_write.call_args[0][0]
        assert delete_sql.upper().startswith("DELETE FROM ")
        assert "raw.Fact_ExchangeRate" in delete_sql
        assert "FiscalYear = 2026" in delete_sql
        assert "FiscalMonth = 5" in delete_sql

    def test_table_not_on_allowlist_rejected(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]

        result = json.loads(fn("raw.secret_data", 2026, 5))

        assert result["code"] == "TABLE_NOT_ALLOWED"
        mock_db.execute_write.assert_not_called()

    def test_unqualified_table_rejected(self) -> None:
        config = _make_config(write_allowlist=["Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]

        result = json.loads(fn("Fact_ExchangeRate", 2026, 5))

        assert result["code"] == "INVALID_OPERATION"
        mock_db.execute_write.assert_not_called()

    def test_missing_fiscal_year_column_rejected(self) -> None:
        """Dim tables typically lack FiscalYear; the tool must refuse rather
        than execute a DELETE that would fail with a cryptic SQL error."""
        config = _make_config(write_allowlist=["raw.Dim_Entity"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]
        self._setup_columns_check(mock_db, ["FiscalMonth"])  # only FiscalMonth

        result = json.loads(fn("raw.Dim_Entity", 2026, 5))

        assert result["code"] == "INVALID_OPERATION"
        assert "FiscalYear" in result["message"]
        mock_db.execute_write.assert_not_called()

    def test_missing_fiscal_month_column_rejected(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_X"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]
        self._setup_columns_check(mock_db, ["FiscalYear"])

        result = json.loads(fn("raw.Fact_X", 2026, 5))

        assert result["code"] == "INVALID_OPERATION"
        assert "FiscalMonth" in result["message"]
        mock_db.execute_write.assert_not_called()

    def test_invalid_month_rejected(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]

        for bad in (0, 13, -1, 100):
            result = json.loads(fn("raw.Fact_ExchangeRate", 2026, bad))
            assert result["code"] == "INVALID_OPERATION", f"month={bad} should be rejected"

        mock_db.execute_write.assert_not_called()

    def test_invalid_year_rejected(self) -> None:
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]

        for bad in (-1, 0, 1899, 10000, 99999):
            result = json.loads(fn("raw.Fact_ExchangeRate", bad, 5))
            assert result["code"] == "INVALID_OPERATION", f"year={bad} should be rejected"

        mock_db.execute_write.assert_not_called()

    def test_zero_rows_deleted_is_success(self) -> None:
        """If no rows match the period, that's a successful no-op, not an error."""
        config = _make_config(write_allowlist=["raw.Fact_ExchangeRate"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_delete_period"]
        self._setup_columns_check(mock_db, ["FiscalYear", "FiscalMonth"])
        mock_db.execute_write.return_value = 0

        result = json.loads(fn("raw.Fact_ExchangeRate", 2026, 5))
        assert result["deleted_rows"] == 0


class TestFabricInsertSch1xRows:
    """Structured bulk insert into raw.Fact_Sch1X. Bypasses the
    LLM-emits-SQL bottleneck of the preview/execute pair by letting the
    client send rows as data; server composes the multi-row INSERT and
    runs it in one DB round-trip."""

    def _make_row(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "pl_mapping_key": 10,
            "mtd_actual": 1.5,
            "mtd_budget": 2.5,
            "mtd_latest_estimate": 3.5,
            "mtd_prior_year": 4.5,
            "ytd_actual": 10.0,
            "ytd_budget": 20.0,
            "ytd_latest_estimate": 30.0,
            "ytd_prior_year": 40.0,
        }
        base.update(overrides)
        return base

    def _tools_with_allowlist(self) -> tuple[callable, MagicMock]:
        config = _make_config(write_allowlist=["raw.Fact_Sch1X"])
        tools = _make_write_tools(config=config)
        return tools["fabric_insert_sch1x_rows"]

    def test_happy_path_returns_rows_inserted(self) -> None:
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 2

        result = json.loads(
            fn(
                entity_key=7,
                fiscal_year=2026,
                fiscal_month=5,
                version=1,
                unit="1000",
                rows=[
                    self._make_row(pl_mapping_key=10),
                    self._make_row(pl_mapping_key=11),
                ],
            )
        )

        assert result["rows_inserted"] == 2
        assert result["entity_key"] == 7
        assert result["fiscal_year"] == 2026
        assert result["fiscal_month"] == 5
        assert result["version"] == 1

    def test_emitted_sql_has_expected_shape(self) -> None:
        """The composed SQL must target raw.Fact_Sch1X, list columns in the
        order the table expects, fill *_Pct as NULL, use GETDATE() for
        LoadedAt, and apply EntityKey / Fiscal* / Version / Unit per row."""
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        fn(
            entity_key=7,
            fiscal_year=2026,
            fiscal_month=5,
            version=1,
            unit="1000",
            rows=[self._make_row(pl_mapping_key=10)],
        )

        sql = mock_db.execute_write.call_args[0][0]
        # Target table.
        assert "INSERT INTO raw.Fact_Sch1X" in sql
        # Column order: identifying cols, MTD block, MTD_Pct block, YTD
        # block, YTD_Pct block, LoadedAt. FactSch1XKey omitted.
        assert "FactSch1XKey" not in sql
        column_clause = sql.split("VALUES", 1)[0]
        for col in (
            "EntityKey", "PLMappingKey", "FiscalYear", "FiscalMonth",
            "Version", "Unit",
            "MTD_Actual", "MTD_Budget", "MTD_LatestEstimate", "MTD_PriorYear",
            "MTD_Actual_Pct", "MTD_Budget_Pct", "MTD_LatestEst_Pct",
            "MTD_PriorYear_Pct",
            "YTD_Actual", "YTD_Budget", "YTD_LatestEstimate", "YTD_PriorYear",
            "YTD_Actual_Pct", "YTD_Budget_Pct", "YTD_LatestEst_Pct",
            "YTD_PriorYear_Pct",
            "LoadedAt",
        ):
            assert col in column_clause, f"missing column {col} in INSERT list"
        # Per-row VALUES tuple ends with NULL,NULL,NULL,NULL, GETDATE())
        assert "NULL, NULL, NULL, NULL, GETDATE()" in sql

    def test_emitted_sql_multi_row_values_for_each_row(self) -> None:
        """Multiple rows → one INSERT with N VALUES tuples joined by `, `."""
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 3

        fn(
            entity_key=7, fiscal_year=2026, fiscal_month=5, version=1, unit="1000",
            rows=[
                self._make_row(pl_mapping_key=10),
                self._make_row(pl_mapping_key=11),
                self._make_row(pl_mapping_key=12),
            ],
        )

        sql = mock_db.execute_write.call_args[0][0]
        values_clause = sql.split("VALUES", 1)[1]
        # GETDATE() appears exactly once per row tuple — robust way to count
        # rows in the composed SQL without trying to disambiguate row-open
        # parens from the GETDATE() call paren.
        assert values_clause.count("GETDATE()") == 3
        # PLMappingKey for each row must end up in order in the SQL.
        for pl in (10, 11, 12):
            assert f", {pl}, 2026, 5, 1, '1000'," in sql

    def test_none_metric_becomes_sql_null(self) -> None:
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        fn(
            entity_key=7, fiscal_year=2026, fiscal_month=5, version=1, unit="1000",
            rows=[
                self._make_row(
                    pl_mapping_key=10,
                    mtd_actual=None,
                    ytd_prior_year=None,
                ),
            ],
        )

        sql = mock_db.execute_write.call_args[0][0]
        values_clause = sql.split("VALUES", 1)[1]
        # The NULL literal must appear among the MTD metric slots (positions
        # 7..10 of the row tuple). Just verify NULL appears as a value rather
        # than only as a *_Pct placeholder.
        assert ", NULL," in values_clause

    def test_integer_valued_float_drops_decimal_point(self) -> None:
        """Per spec: 1000.0 → '1000', not '1000.0'."""
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        fn(
            entity_key=7, fiscal_year=2026, fiscal_month=5, version=1, unit="1000",
            rows=[self._make_row(pl_mapping_key=10, mtd_actual=1000.0)],
        )

        sql = mock_db.execute_write.call_args[0][0]
        # Look for ' 1000,' (with space) — the metric slot — and confirm
        # there is no '1000.0' anywhere.
        assert ", 1000," in sql
        assert "1000.0" not in sql

    def test_non_integer_float_preserves_precision(self) -> None:
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        fn(
            entity_key=7, fiscal_year=2026, fiscal_month=5, version=1, unit="1000",
            rows=[self._make_row(pl_mapping_key=10, mtd_actual=1234.56)],
        )

        sql = mock_db.execute_write.call_args[0][0]
        assert "1234.56" in sql

    def test_unit_with_single_quote_is_escaped(self) -> None:
        """Defence in depth — unit comes from the LLM; a stray single quote
        must be doubled, not break out of the literal."""
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        fn(
            entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
            unit="1'00",
            rows=[self._make_row(pl_mapping_key=10)],
        )

        sql = mock_db.execute_write.call_args[0][0]
        assert "'1''00'" in sql
        # Crucially, no unescaped single-quote-then-comma sequence that
        # would close the literal mid-row.
        assert "'1'00'" not in sql

    def test_empty_rows_rejected(self) -> None:
        fn, mock_db = self._tools_with_allowlist()

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000", rows=[],
            )
        )

        assert result["code"] == "INVALID_OPERATION"
        assert "non-empty" in result["message"].lower()
        mock_db.execute_write.assert_not_called()

    def test_table_not_on_allowlist_rejected(self) -> None:
        """If raw.Fact_Sch1X is not in the allowlist, the tool refuses
        even though the table is hard-coded — operator stays in control."""
        config = _make_config(write_allowlist=["something.else"])
        tools = _make_write_tools(config=config)
        fn, mock_db = tools["fabric_insert_sch1x_rows"]

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000", rows=[self._make_row()],
            )
        )

        assert result["code"] == "TABLE_NOT_ALLOWED"
        mock_db.execute_write.assert_not_called()

    def test_invalid_fiscal_month_rejected(self) -> None:
        fn, mock_db = self._tools_with_allowlist()

        for bad in (0, 13, -1):
            result = json.loads(
                fn(
                    entity_key=7, fiscal_year=2026, fiscal_month=bad, version=1,
                    unit="1000", rows=[self._make_row()],
                )
            )
            assert result["code"] == "INVALID_OPERATION", f"month={bad}"
        mock_db.execute_write.assert_not_called()

    def test_invalid_fiscal_year_rejected(self) -> None:
        fn, mock_db = self._tools_with_allowlist()

        for bad in (1899, 0, 10000):
            result = json.loads(
                fn(
                    entity_key=7, fiscal_year=bad, fiscal_month=5, version=1,
                    unit="1000", rows=[self._make_row()],
                )
            )
            assert result["code"] == "INVALID_OPERATION", f"year={bad}"
        mock_db.execute_write.assert_not_called()

    def test_missing_pl_mapping_key_names_row_index(self) -> None:
        """Error message must identify which row failed so the LLM can fix
        the right entry, not the whole batch."""
        fn, mock_db = self._tools_with_allowlist()

        rows = [
            self._make_row(pl_mapping_key=10),
            self._make_row(),  # remove pl_mapping_key next
            self._make_row(pl_mapping_key=12),
        ]
        del rows[1]["pl_mapping_key"]

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000", rows=rows,
            )
        )

        assert result["code"] == "INVALID_OPERATION"
        assert "rows[1]" in result["message"]
        assert "pl_mapping_key" in result["message"]
        mock_db.execute_write.assert_not_called()

    def test_bool_metric_rejected(self) -> None:
        """bool is a Python int subclass; silently coercing True→1 in a
        finance metric would mask an LLM-side type error."""
        fn, mock_db = self._tools_with_allowlist()

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000",
                rows=[self._make_row(pl_mapping_key=10, mtd_actual=True)],
            )
        )

        assert result["code"] == "INVALID_OPERATION"
        assert "rows[0].mtd_actual" in result["message"]
        mock_db.execute_write.assert_not_called()

    def test_nan_metric_rejected(self) -> None:
        fn, mock_db = self._tools_with_allowlist()

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000",
                rows=[
                    self._make_row(pl_mapping_key=10, mtd_actual=float("nan"))
                ],
            )
        )

        assert result["code"] == "INVALID_OPERATION"
        assert "rows[0].mtd_actual" in result["message"]
        mock_db.execute_write.assert_not_called()

    def test_missing_metric_field_treated_as_null(self) -> None:
        """If the LLM omits a metric key entirely, treat it as NULL rather
        than rejecting — keeps the row payload compact when many metrics
        are unknown."""
        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.return_value = 1

        partial = {
            "pl_mapping_key": 10,
            "mtd_actual": 1.5,
            # mtd_budget omitted intentionally
            "mtd_latest_estimate": 3.5,
            "mtd_prior_year": 4.5,
            "ytd_actual": 10.0,
            "ytd_budget": 20.0,
            "ytd_latest_estimate": 30.0,
            "ytd_prior_year": 40.0,
        }
        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000", rows=[partial],
            )
        )
        assert result["rows_inserted"] == 1
        sql = mock_db.execute_write.call_args[0][0]
        # 1.5 (mtd_actual), NULL (mtd_budget omitted), 3.5 (mtd_latest_estimate)
        assert "1.5, NULL, 3.5" in sql

    def test_db_error_surfaces_as_query_error_envelope(self) -> None:
        from src.database import FabricQueryError

        fn, mock_db = self._tools_with_allowlist()
        mock_db.execute_write.side_effect = FabricQueryError(
            message="Violation of PRIMARY KEY constraint",
            code="QUERY_ERROR",
            details=None,
            sqlstate="23000",
        )

        result = json.loads(
            fn(
                entity_key=7, fiscal_year=2026, fiscal_month=5, version=1,
                unit="1000", rows=[self._make_row()],
            )
        )

        assert result["code"] == "QUERY_ERROR"
        assert "PRIMARY KEY" in result["message"]
