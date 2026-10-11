"""A filter reply whose text holds a lone UTF-16 surrogate is a bad reply
(review: valid JSON like "hi \\ud83d" decoded to text that can't be encoded,
so it broke the paste, History and every event stream)."""

from __future__ import annotations

import pytest

from services.automation import filters
from services.automation.models import BEFORE_CLEANUP
from tests import test_automation_hooks_filters as base
from tests.test_automation_hooks_filters import (
    TEXT,
    Answer,
    http_filter,
    job,
    program_filter,
    settings_with,
    wait_for,
)

receiver = base.receiver
failures = base.failures
no_leases = base.no_leases

REASON = "\"text\" wasn't valid Unicode"


@pytest.mark.parametrize("body", [
    b'{"text": "hi \\ud83d"}',          # a high surrogate cut from its pair
    b'{"text": "\\ude00 there"}',        # a lone low surrogate
    b'{"text": "a\\udfff\\ud800b"}',     # a pair in the wrong order
])
def test_an_http_reply_with_a_lone_surrogate_keeps_the_text(receiver, failures, body):
    receiver.answer("/f", Answer(body=body))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(http_filter("f", receiver.url("/f"))))
    assert outcome == filters.FilterOutcome(TEXT)
    assert wait_for(failures, 1)[0].data["reason"] == REASON


def test_a_program_reply_with_a_lone_surrogate_keeps_the_text(failures):
    code = "import sys; sys.stdin.read(); sys.stdout.write('{\"text\": \"x \\\\ud83d\"}')"
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings_with(program_filter("p", code)))
    assert outcome == filters.FilterOutcome(TEXT)
    assert wait_for(failures, 1)[0].data["reason"] == REASON


def test_a_whole_surrogate_pair_is_an_ordinary_character(receiver, failures):
    receiver.answer("/f", Answer(body=b'{"text": "done \\ud83d\\ude00"}'))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(http_filter("f", receiver.url("/f"))))
    assert outcome == filters.FilterOutcome("done \N{GRINNING FACE}", changed=True)
    assert failures == []
