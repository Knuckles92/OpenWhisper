"""No words are not a filter cancel (review: an empty transcript was reported as
"Not pasted — a text filter handled it"). ``FilterOutcome.canceled`` alone says
a filter took over delivery."""

from __future__ import annotations

import pytest

from services.automation import filters
from services.automation.models import AFTER_CLEANUP, BEFORE_CLEANUP
from tests import test_automation_hooks_filters as base
from tests.test_automation_hooks_filters import TEXT, Answer, http_filter, job, settings_with

receiver = base.receiver
no_leases = base.no_leases


@pytest.mark.parametrize("stage", [BEFORE_CLEANUP, AFTER_CLEANUP])
@pytest.mark.parametrize("silence", ["", " ", "\n\t"])
def test_no_words_with_filters_configured_is_not_a_cancel(receiver, stage, silence):
    receiver.answer("/f", Answer(body=b'{"cancel": true}'))
    outcome = filters.apply(stage, silence, job(),
                            settings_with(http_filter("f", receiver.url("/f"), stage)))
    assert outcome == filters.FilterOutcome(silence)
    assert outcome.canceled is False
    assert receiver.arrivals == []


@pytest.mark.parametrize("body", [b'{"text": ""}', b'{"text": " \\n "}', b'{"cancel": true}'])
def test_only_a_filter_that_empties_or_cancels_the_words_cancels(receiver, body):
    receiver.answer("/f", Answer(body=body))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(http_filter("f", receiver.url("/f"))))
    assert outcome.canceled is True
