"""GitHub webhook HMAC verification. Constant-time compare; rejects missing/garbage headers."""
from __future__ import annotations

import hashlib
import hmac


def compute_signature(secret: str, body: bytes) -> str:
    """The `X-Hub-Signature-256` value GitHub would send for this body."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, header)
