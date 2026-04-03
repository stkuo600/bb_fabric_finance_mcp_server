"""Unit tests for API key token verifier."""

from __future__ import annotations

import pytest

from src.token_verifier import ApiKeyTokenVerifier


class TestApiKeyTokenVerifier:
    """Test ApiKeyTokenVerifier."""

    @pytest.fixture
    def verifier(self) -> ApiKeyTokenVerifier:
        return ApiKeyTokenVerifier(api_key="correct-key-123")

    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(self, verifier: ApiKeyTokenVerifier) -> None:
        result = await verifier.verify_token("correct-key-123")
        assert result is not None
        assert result.token == "correct-key-123"
        assert result.client_id == "api-key-client"
        assert result.scopes == []

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self, verifier: ApiKeyTokenVerifier) -> None:
        result = await verifier.verify_token("wrong-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_token_returns_none(self, verifier: ApiKeyTokenVerifier) -> None:
        result = await verifier.verify_token("")
        assert result is None
