"""Unit tests for MSAL OAuth2 authentication."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.auth import FabricAuth


class TestFabricAuth:
    """Test OAuth2 token acquisition and refresh."""

    def _make_auth(self) -> FabricAuth:
        with patch("src.auth.msal.ConfidentialClientApplication"):
            return FabricAuth(
                tenant_id="test-tenant",
                client_id="test-client",
                client_secret="test-secret",
            )

    def test_get_token_from_cache(self) -> None:
        auth = self._make_auth()
        auth._app.acquire_token_silent.return_value = {"access_token": "cached-token"}

        token = auth.get_token()

        assert token == "cached-token"
        auth._app.acquire_token_silent.assert_called_once()
        auth._app.acquire_token_for_client.assert_not_called()

    def test_get_token_fresh_when_cache_empty(self) -> None:
        auth = self._make_auth()
        auth._app.acquire_token_silent.return_value = None
        auth._app.acquire_token_for_client.return_value = {"access_token": "fresh-token"}

        token = auth.get_token()

        assert token == "fresh-token"
        auth._app.acquire_token_for_client.assert_called_once()

    def test_get_token_raises_on_failure(self) -> None:
        auth = self._make_auth()
        auth._app.acquire_token_silent.return_value = None
        auth._app.acquire_token_for_client.return_value = {
            "error": "invalid_client",
            "error_description": "Client secret is invalid",
        }

        with pytest.raises(RuntimeError, match="AUTH_FAILED"):
            auth.get_token()

    def test_failure_does_not_leak_aadsts_detail_to_client(self) -> None:
        """The client-facing RuntimeError must not carry raw MSAL/AADSTS text
        (error codes, trace/correlation IDs) — that is reconnaissance-grade
        identity disclosure. Keep code AUTH_FAILED and a generic message (P12)."""
        auth = self._make_auth()
        auth._app.acquire_token_silent.return_value = None
        auth._app.acquire_token_for_client.return_value = {
            "error": "invalid_client",
            "error_description": (
                "AADSTS7000215: Invalid client secret provided. "
                "Trace ID: abc-123 Correlation ID: def-456"
            ),
        }

        with pytest.raises(RuntimeError) as exc_info:
            auth.get_token()

        text = str(exc_info.value)
        assert "AUTH_FAILED" in text, "stable code must be preserved for callers"
        assert "AADSTS7000215" not in text, "raw AADSTS code must not reach the client"
        assert "abc-123" not in text and "def-456" not in text, (
            "trace/correlation IDs must not reach the client"
        )

    def test_failure_logs_full_msal_detail_server_side(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The full MSAL detail must still be available server-side for
        diagnosis — log server-side, scrub client-side."""
        import logging

        auth = self._make_auth()
        auth._app.acquire_token_silent.return_value = None
        auth._app.acquire_token_for_client.return_value = {
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret. Trace ID: abc-123",
        }

        with caplog.at_level(logging.ERROR, logger="fabric_mcp.auth"), pytest.raises(RuntimeError):
            auth.get_token()

        logged = caplog.text
        assert "AADSTS7000215" in logged, "full MSAL detail must be retained server-side"

    def test_authority_uses_tenant_id(self) -> None:
        auth = self._make_auth()
        assert auth._authority == "https://login.microsoftonline.com/test-tenant"

    def test_scope_is_powerbi_api(self) -> None:
        auth = self._make_auth()
        assert auth._scopes == ["https://analysis.windows.net/powerbi/api/.default"]
