"""Token authentication for the meeting dashboard.

Two capability tokens exist per meeting: a host token and a guest token
(128+ bits each, URL-safe). Role resolution uses constant-time comparison so
token checking is not a timing oracle. The host token is never included in
guest-facing payloads.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

ROLE_HOST = "host"
ROLE_GUEST = "guest"

#: Entropy per token in bytes (256 bits).
TOKEN_BYTES = 32


def generate_token() -> str:
    """Return a new URL-safe capability token.

    Returns:
        A ``secrets.token_urlsafe`` string with ``TOKEN_BYTES`` of entropy.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def generate_token_pair() -> Tuple[str, str]:
    """Return a fresh ``(host_token, guest_token)`` pair.

    Used at meeting creation and for one-click link regeneration.
    """
    return generate_token(), generate_token()


def _matches(candidate: str, expected: Optional[str]) -> bool:
    """Constant-time equality between a presented and an expected token."""
    if not expected:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8"), str(expected).encode("utf-8")
    )


def resolve_role(token: Optional[str], host_token: Optional[str],
                 guest_token: Optional[str]) -> Optional[str]:
    """Resolve a presented token to a dashboard role.

    Args:
        token: The token presented by the client (path segment or query).
        host_token: The meeting's current host token.
        guest_token: The meeting's current guest token.

    Returns:
        ``'host'`` or ``'guest'`` on a match, ``None`` for anything else.
    """
    if not token or not isinstance(token, str):
        return None
    if _matches(token, host_token):
        return ROLE_HOST
    if _matches(token, guest_token):
        return ROLE_GUEST
    return None
