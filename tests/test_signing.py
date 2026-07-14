import hashlib
import hmac

import pytest

from zolva.signing import SignatureError, sign_payload, verify_zolva_signature


def test_round_trip_verifies() -> None:
    ts, sig = sign_payload("secret", b"body")
    verify_zolva_signature(b"body", sig, ts, "secret", now=int(ts))


def test_tampered_body_raises() -> None:
    ts, sig = sign_payload("secret", b"body")
    with pytest.raises(SignatureError):
        verify_zolva_signature(b"tampered", sig, ts, "secret", now=int(ts))


def test_wrong_secret_raises() -> None:
    ts, sig = sign_payload("secret", b"body")
    with pytest.raises(SignatureError):
        verify_zolva_signature(b"body", sig, ts, "wrong-secret", now=int(ts))


def test_timestamp_outside_tolerance_raises() -> None:
    ts, sig = sign_payload("secret", b"body")
    with pytest.raises(SignatureError):
        verify_zolva_signature(b"body", sig, ts, "secret", now=int(ts) + 301)


def test_non_numeric_timestamp_raises() -> None:
    with pytest.raises(SignatureError):
        verify_zolva_signature(b"body", "deadbeef", "not-a-number", "secret")


def test_wire_format_pin() -> None:
    ts, sig = sign_payload("s", b"b")
    expected = hmac.new(b"s", ts.encode() + b"." + b"b", hashlib.sha256).hexdigest()
    assert sig == expected
