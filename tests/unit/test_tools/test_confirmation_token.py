"""Unit tests for the confirmation-token subdomain.

Covers the typed payload (``ConfirmationTokenPayload``), the issuer
(``make_confirmation_token``), the verifier (``parse_confirmation_token``)
and the expiry check (``ensure_token_not_expired``). The split between
parse and expiry mirrors the actual concerns: signature validity is
timeless, expiry is time-sensitive.

The wire format is asserted at the round-trip level — any change to the
token shape would break tokens already in flight, so the test is the
guard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.tools._confirmation_token import (
    _CURRENT_TOKEN_VERSION,
    ConfirmationTokenPayload,
    ensure_token_not_expired,
    make_confirmation_token,
    parse_confirmation_token,
)
from src.tools._responses import ToolInputError


def _fixed_now() -> datetime:
    return datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


class TestMakeConfirmationToken:
    """make_confirmation_token encapsulates v / iat / exp / nonce policy.
    Callers supply only the operational fields (sql / op / table) +
    secret + expires_in."""

    def test_returns_token_string_and_expires_at(self) -> None:
        token, expires_at = make_confirmation_token(
            sql="INSERT INTO gold.t (id) VALUES (1)",
            op="INSERT",
            table="gold.t",
            secret="s3cret",
            expires_in=timedelta(minutes=5),
            now=_fixed_now(),
        )
        assert isinstance(token, str)
        assert token.count(".") == 1  # payload.sig shape preserved
        assert expires_at == _fixed_now() + timedelta(minutes=5)

    def test_token_is_round_trippable(self) -> None:
        """A token built by make_* must parse back to the same fields."""
        token, _ = make_confirmation_token(
            sql="UPDATE gold.t SET c=1",
            op="UPDATE",
            table="gold.t",
            secret="s3cret",
            expires_in=timedelta(minutes=5),
            now=_fixed_now(),
        )
        payload = parse_confirmation_token(token, "s3cret")
        assert payload.sql == "UPDATE gold.t SET c=1"
        assert payload.op == "UPDATE"
        assert payload.table == "gold.t"
        assert payload.v == _CURRENT_TOKEN_VERSION
        assert payload.iat == _fixed_now().timestamp()
        assert payload.exp == (_fixed_now() + timedelta(minutes=5)).timestamp()
        assert len(payload.nonce) >= 16

    def test_distinct_calls_produce_distinct_nonces(self) -> None:
        """Replay protection depends on each token being unique even
        when the operational fields are identical."""
        kw = dict(
            sql="INSERT INTO gold.t (id) VALUES (1)",
            op="INSERT",
            table="gold.t",
            secret="s3cret",
            expires_in=timedelta(minutes=5),
            now=_fixed_now(),
        )
        t1, _ = make_confirmation_token(**kw)
        t2, _ = make_confirmation_token(**kw)
        assert t1 != t2  # nonce differs

    def test_now_defaults_to_real_clock_when_omitted(self) -> None:
        """In production preview_write does not pass `now` explicitly."""
        before = datetime.now(tz=UTC).timestamp()
        token, _ = make_confirmation_token(
            sql="INSERT INTO gold.t (id) VALUES (1)",
            op="INSERT",
            table="gold.t",
            secret="s3cret",
            expires_in=timedelta(minutes=5),
        )
        after = datetime.now(tz=UTC).timestamp()
        payload = parse_confirmation_token(token, "s3cret")
        assert before <= payload.iat <= after


class TestParseConfirmationToken:
    """parse raises TOKEN_INVALID on every flavour of failure — bad
    shape, bad sig, bad payload — so callers handle one error code."""

    def _good_token(self) -> str:
        token, _ = make_confirmation_token(
            sql="INSERT INTO gold.t (id) VALUES (1)",
            op="INSERT",
            table="gold.t",
            secret="s3cret",
            expires_in=timedelta(minutes=5),
            now=_fixed_now(),
        )
        return token

    def test_happy_path_returns_typed_payload(self) -> None:
        payload = parse_confirmation_token(self._good_token(), "s3cret")
        assert isinstance(payload, ConfirmationTokenPayload)

    def test_non_string_token_rejected(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token(None, "s3cret")  # type: ignore[arg-type]
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_malformed_token_no_separator_rejected(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token("no-dot-here", "s3cret")
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_malformed_token_too_many_separators_rejected(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token("a.b.c", "s3cret")
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_garbage_base64_rejected(self) -> None:
        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token("$$$.$$$", "s3cret")
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_wrong_secret_rejected(self) -> None:
        """Cross-secret separation: a token signed with secret A must
        not be redeemable with secret B."""
        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token(self._good_token(), "wrong-secret")
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_version_mismatch_rejected(self) -> None:
        """A token bearing v != _CURRENT_TOKEN_VERSION must be rejected
        so an old replica's tokens can be invalidated on schema bumps."""
        import base64
        import hashlib
        import hmac

        # Forge a payload with v=999 + a valid signature with the same secret.
        secret = "s3cret"
        payload_dict = {
            "v": 999,
            "sql": "x",
            "op": "INSERT",
            "table": "x",
            "iat": 0.0,
            "exp": 9_999_999_999.0,
            "nonce": "00",
        }
        payload_bytes = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        key = hashlib.sha256(b"fabric-mcp-write-confirmation\x00" + secret.encode()).digest()
        sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
        token = f"{payload_b64}.{sig_b64}"

        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token(token, secret)
        assert exc_info.value.code == "TOKEN_INVALID"

    def test_missing_required_field_rejected(self) -> None:
        """A payload missing e.g. `sql` must be rejected — without
        Pydantic validation we'd silently coerce it to '' and execute."""
        import base64
        import hashlib
        import hmac

        secret = "s3cret"
        payload_dict = {
            "v": _CURRENT_TOKEN_VERSION,
            # NB: sql missing
            "op": "INSERT",
            "table": "x",
            "iat": 0.0,
            "exp": 9_999_999_999.0,
            "nonce": "00",
        }
        payload_bytes = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        key = hashlib.sha256(b"fabric-mcp-write-confirmation\x00" + secret.encode()).digest()
        sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
        token = f"{payload_b64}.{sig_b64}"

        with pytest.raises(ToolInputError) as exc_info:
            parse_confirmation_token(token, secret)
        assert exc_info.value.code == "TOKEN_INVALID"


class TestEnsureTokenNotExpired:
    """Expiry is a separate concern from parse so callers can:
    - test the two failures independently
    - inject `now` deterministically
    - decide whether to preserve operation/table on the expired-error UX
      (batch does; execute_write doesn't care)."""

    def _payload(self, exp: float) -> ConfirmationTokenPayload:
        return ConfirmationTokenPayload(
            v=_CURRENT_TOKEN_VERSION,
            sql="x",
            op="INSERT",
            table="x",
            iat=0.0,
            exp=exp,
            nonce="00",
        )

    def test_not_expired_returns_none_silently(self) -> None:
        now = 1000.0
        payload = self._payload(exp=now + 60)
        assert ensure_token_not_expired(payload, now=now) is None

    def test_expired_raises_token_expired(self) -> None:
        now = 1000.0
        payload = self._payload(exp=now - 1)
        with pytest.raises(ToolInputError) as exc_info:
            ensure_token_not_expired(payload, now=now)
        assert exc_info.value.code == "TOKEN_EXPIRED"

    def test_exp_at_now_is_treated_as_expired(self) -> None:
        """Boundary: exp == now should reject. Otherwise a token issued
        at second 0 with expires_in=0 would be valid for a full second."""
        now = 1000.0
        payload = self._payload(exp=now)
        with pytest.raises(ToolInputError):
            ensure_token_not_expired(payload, now=now)
