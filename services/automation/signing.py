"""Standard Webhooks signatures (https://www.standardwebhooks.com).

The same scheme MCP Events uses: ``webhook-id``, ``webhook-timestamp`` and
``webhook-signature: v1,<base64 HMAC-SHA256>`` over ``id.timestamp.body``,
keyed by the bytes behind a ``whsec_`` secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from typing import Mapping, Optional

PREFIX = "whsec_"


def new_secret() -> str:
    return PREFIX + base64.b64encode(secrets.token_bytes(24)).decode("ascii")


def _key(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith(PREFIX):
        raise ValueError("Webhook secrets start with whsec_.")
    try:
        key = base64.b64decode(secret[len(PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The webhook secret is not valid base64.") from exc
    if not key:
        raise ValueError("The webhook secret is empty.")
    return key


def signature(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{int(timestamp)}.".encode("utf-8") + bytes(body)
    digest = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def headers(secret: str, msg_id: str, body: bytes, *,
            timestamp: Optional[int] = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": signature(secret, msg_id, ts, body),
    }


def verify(secret: str, received: Mapping[str, str], body: bytes, *,
           tolerance_s: int = 300, now: Optional[int] = None) -> bool:
    """True when one of the ``v1`` signatures matches and the timestamp is fresh."""
    lowered = {str(k).lower(): str(v) for k, v in (received or {}).items()}
    msg_id = lowered.get("webhook-id", "")
    try:
        ts = int(lowered.get("webhook-timestamp", ""))
    except ValueError:
        return False
    current = int(time.time()) if now is None else int(now)
    if abs(current - ts) > tolerance_s or not msg_id:
        return False
    try:
        expected = signature(secret, msg_id, ts, body).split(",", 1)[1]
    except ValueError:
        return False
    for part in lowered.get("webhook-signature", "").split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False
