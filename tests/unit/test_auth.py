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

    def test_authority_uses_tenant_id(self) -> None:
        auth = self._make_auth()
        assert auth._authority == "https://login.microsoftonline.com/test-tenant"

    def test_scope_is_powerbi_api(self) -> None:
        auth = self._make_auth()
        assert auth._scopes == ["https://analysis.windows.net/powerbi/api/.default"]
