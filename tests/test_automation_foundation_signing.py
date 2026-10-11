"""Standard Webhooks signatures, the scheme webhooks and MCP Events share."""

import pytest

from services.automation import signing

# The published Standard Webhooks example (standard-webhooks/standard-webhooks README).
SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
MSG_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
TIMESTAMP = 1614265330
BODY = b'{"test": 2432232314}'
EXPECTED = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_matches_the_published_example():
    assert signing.signature(SECRET, MSG_ID, TIMESTAMP, BODY) == EXPECTED


def test_headers_round_trip_through_verify():
    secret = signing.new_secret()
    assert secret.startswith("whsec_")
    headers = signing.headers(secret, "evt_1", b"{}", timestamp=1000)
    assert set(headers) == {"webhook-id", "webhook-timestamp", "webhook-signature"}
    assert signing.verify(secret, headers, b"{}", now=1000)
    assert not signing.verify(secret, headers, b"{ }", now=1000)
    assert not signing.verify(signing.new_secret(), headers, b"{}", now=1000)
    assert not signing.verify(secret, headers, b"{}", now=1000 + 301)


def test_verify_accepts_one_of_several_signatures():
    headers = {
        "Webhook-Id": MSG_ID,
        "Webhook-Timestamp": str(TIMESTAMP),
        "Webhook-Signature": "v1,bogus " + EXPECTED,
    }
    assert signing.verify(SECRET, headers, BODY, now=TIMESTAMP)


@pytest.mark.parametrize("secret", ["", "MfKQ9r8", "whsec_", "whsec_!!!"])
def test_bad_secrets_are_refused(secret):
    with pytest.raises(ValueError):
        signing.signature(secret, "id", 1, b"")
    assert not signing.verify(secret, {"webhook-id": "id", "webhook-timestamp": "1",
                                       "webhook-signature": "v1,x"}, b"", now=1)
