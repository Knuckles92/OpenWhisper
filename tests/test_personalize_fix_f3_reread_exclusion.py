"""The learning re-read checks the site in front when it runs, not at dictation."""

import threading
from dataclasses import replace

import pytest

from services.focus_context import AppIdentity
from services.focus_context._capture import CaptureService
from tests.test_personalize_s1_service import CONTEXT, FakePlatform

# Browser tabs share the window and the browser's pid; only the site differs.
GMAIL = AppIdentity("chrome.exe", "Chrome", pid=20, window="0x5", title_hint="Gmail",
                    platform="windows")
WHATSAPP = replace(GMAIL, title_hint="WhatsApp")
GITHUB = replace(GMAIL, title_hint="GitHub")
WAIT = 5.0


@pytest.fixture
def build():
    services = []

    def make(current, excluded):
        platform = FakePlatform(current)
        service = CaptureService(
            platform, deadline_s=1.0,
            settings=lambda: {"app_context_excluded_apps": list(excluded)},
            metrics=lambda **_values: None,
        )
        services.append(service)
        return service, platform

    yield make
    for service in services:
        service.shutdown()


def _reread(service, identity):
    answers = []
    done = threading.Event()

    def callback(context):
        answers.append(context)
        done.set()

    service.reread(identity, callback)
    assert done.wait(WAIT)
    return answers[0]


def test_a_tab_switch_to_an_excluded_site_reads_nothing(build):
    service, platform = build(WHATSAPP, ["WhatsApp"])

    assert _reread(service, GMAIL) is None
    assert platform.reads == []


def test_a_tab_switch_to_another_site_reads_nothing(build):
    service, platform = build(GITHUB, [])

    assert _reread(service, GMAIL) is None
    assert platform.reads == []


def test_a_site_excluded_after_the_dictation_is_not_reread(build):
    excluded = []
    service, platform = build(GMAIL, excluded)
    assert _reread(service, GMAIL) == CONTEXT

    excluded.append("Gmail")

    assert _reread(service, GMAIL) is None
    assert len(platform.reads) == 1


def test_the_same_tab_is_still_reread(build):
    service, platform = build(GMAIL, ["WhatsApp"])

    assert _reread(service, GMAIL) == CONTEXT
    assert platform.reads == [("chrome.exe", True, True)]
