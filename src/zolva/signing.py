"""Zolva webhook signing: HMAC-SHA256 over "{timestamp}.{body}".

Senders put the hex MAC in X-Zolva-Signature and the unix timestamp in
X-Zolva-Timestamp; the timestamp is inside the MAC, so a captured request
cannot be replayed later. verify_zolva_signature is the receiver-side helper.
"""

from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    """Signature missing, malformed, expired, or mismatched."""


def sign_payload(secret: str, body: bytes, *, now: int | None = None) -> tuple[str, str]:
    """Return (timestamp, hex signature) for an outbound Zolva webhook."""
    ts = str(int(time.time()) if now is None else now)
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, sig


def verify_zolva_signature(
    body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> None:
    """Verify X-Zolva-Signature/X-Zolva-Timestamp. Raises SignatureError on any failure."""
    try:
        ts_int = int(timestamp)
    except ValueError:
        raise SignatureError("malformed X-Zolva-Timestamp") from None
    current = now if now is not None else int(time.time())
    if abs(current - ts_int) > tolerance_seconds:
        raise SignatureError("X-Zolva-Timestamp outside tolerance")
    _, expected = sign_payload(secret, body, now=ts_int)
    if not hmac.compare_digest(signature, expected):
        raise SignatureError("signature mismatch")
