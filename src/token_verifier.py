"""API key token verifier for the MCP server."""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken


class ApiKeyTokenVerifier:
    """Verify bearer tokens against a static API key using constant-time comparison."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._api_key):
            return None
        return AccessToken(token=token, client_id="api-key-client", scopes=[])
