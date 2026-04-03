"""MSAL OAuth2 authentication for Microsoft Fabric SQL endpoint."""

from __future__ import annotations

import logging

import msal

from src.models import ErrorResponse

logger = logging.getLogger("fabric_mcp.auth")

_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


class FabricAuth:
    """Manages OAuth2 client credentials authentication with Azure AD."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._client_id = client_id
        self._scopes = [_SCOPE]
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=self._authority,
        )

    def get_token(self) -> str:
        """Acquire an access token, using cache when available.

        Returns the raw access token string.
        Raises RuntimeError with ErrorResponse details on failure.
        """
        result = self._app.acquire_token_silent(self._scopes, account=None)
        if result and "access_token" in result:
            logger.debug("Token acquired from cache")
            return result["access_token"]

        logger.info("Acquiring new token via client credentials")
        result = self._app.acquire_token_for_client(scopes=self._scopes)

        if "access_token" in result:
            logger.info("Token acquired successfully")
            return result["access_token"]

        error = ErrorResponse(
            code="AUTH_FAILED",
            message=f"Authentication failed: {result.get('error_description', 'Unknown error')}",
            details=result.get("error"),
        )
        raise RuntimeError(error.model_dump_json())
