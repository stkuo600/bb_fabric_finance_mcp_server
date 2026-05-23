"""Confirmation-token subdomain — HMAC-signed stateless token used by the
two-phase write flow (preview → execute).

The token format is a single dotted string ``<payload_b64>.<sig_b64>``:

- ``payload_b64`` is the URL-safe base64 of a UTF-8 JSON object
  matching ``ConfirmationTokenPayload``.
- ``sig_b64`` is the URL-safe base64 of ``HMAC-SHA256(signing_key,
  payload_b64)``. The signing key is derived from the shared
  ``client_secret`` via a domain-separated SHA-256 so an attacker who
  somehow obtains the secret cannot reuse it for another protocol's
  HMAC.

The design is stateless on purpose — see
``.claude/bugfix/2026-05-22-write-token-cross-instance/report.md``.

Public surface:

- ``ConfirmationTokenPayload`` — Pydantic model of the JSON payload.
- ``make_confirmation_token(...) -> (token, expires_at)`` — issuer.
- ``parse_confirmation_token(token, secret) -> ConfirmationTokenPayload``
  — verifier; raises ``ToolInputError(TOKEN_INVALID)`` on any failure.
- ``ensure_token_not_expired(payload, *, now)`` — expiry gate; raises
  ``ToolInputError(TOKEN_EXPIRED)`` if ``payload.exp <= now``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ValidationError

from src.tools._responses import ToolInputError

_CURRENT_TOKEN_VERSION = 1
_SIGNING_KEY_DOMAIN = b"fabric-mcp-write-confirmation\x00"


class ConfirmationTokenPayload(BaseModel):
    """Decoded body of a confirmation token. ``v`` lets us evolve the
    payload schema without breaking already-in-flight tokens — bump
    ``_CURRENT_TOKEN_VERSION`` to invalidate the prior generation."""

    v: int
    sql: str
    op: str
    table: str
    iat: float
    exp: float
    nonce: str


def _signing_key(secret: str) -> bytes:
    return hashlib.sha256(_SIGNING_KEY_DOMAIN + secret.encode("utf-8")).digest()


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def make_confirmation_token(
    *,
    sql: str,
    op: str,
    table: str,
    secret: str,
    expires_in: timedelta,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Issue a signed confirmation token. Returns ``(token, expires_at)``.

    ``now`` defaults to ``datetime.now(tz=UTC)``; explicit injection is
    for deterministic tests. The function owns the v / iat / exp / nonce
    policy so callers cannot accidentally omit replay protection.
    """
    issued_at = now if now is not None else datetime.now(tz=UTC)
    expires_at = issued_at + expires_in
    payload = {
        "v": _CURRENT_TOKEN_VERSION,
        "sql": sql,
        "op": op,
        "table": table,
        "iat": issued_at.timestamp(),
        "exp": expires_at.timestamp(),
        "nonce": secrets.token_hex(16),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    sig = hmac.new(_signing_key(secret), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64u_encode(sig)}", expires_at


def parse_confirmation_token(token: str, secret: str) -> ConfirmationTokenPayload:
    """Verify the signature and decode the payload, or raise
    ``ToolInputError(TOKEN_INVALID)``.

    The single error code is deliberate: signature failure vs.
    malformed-payload vs. version-mismatch must look identical to a
    caller — leaking the distinction would help an attacker probe for
    valid token shapes.
    """
    invalid = ToolInputError(
        code="TOKEN_INVALID",
        message="Confirmation token is missing, malformed, or has an invalid signature.",
    )

    if not isinstance(token, str) or token.count(".") != 1:
        raise invalid

    payload_b64, sig_b64 = token.split(".", 1)
    try:
        sig = _b64u_decode(sig_b64)
    except (ValueError, TypeError) as e:
        raise invalid from e

    expected = hmac.new(_signing_key(secret), payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise invalid

    try:
        payload_bytes = _b64u_decode(payload_b64)
        payload_raw = json.loads(payload_bytes)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise invalid from e

    if not isinstance(payload_raw, dict) or payload_raw.get("v") != _CURRENT_TOKEN_VERSION:
        raise invalid

    try:
        return ConfirmationTokenPayload.model_validate(payload_raw)
    except ValidationError as e:
        raise invalid from e


def ensure_token_not_expired(payload: ConfirmationTokenPayload, *, now: float) -> None:
    """Raise ``ToolInputError(TOKEN_EXPIRED)`` if ``payload.exp <= now``.

    Separate from ``parse_confirmation_token`` so:
    - callers inject ``now`` deterministically without monkeypatching parse
    - the expired-token error UX can decide whether to preserve
      ``payload.op`` / ``payload.table`` (batch does so per-token)
    """
    if payload.exp <= now:
        raise ToolInputError(
            code="TOKEN_EXPIRED",
            message="Confirmation token has expired. Please preview the write operation again.",
        )
