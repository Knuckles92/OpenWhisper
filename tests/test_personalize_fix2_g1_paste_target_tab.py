"""A rewrite started on one site is not pasted into another tab of the same browser.

A browser's tabs share its window and pid; only the site named in the title
(the identity's title_hint) tells them apart.
"""

import pytest

from services import dictation_pipeline, focus_context
from services.dictation_pipeline import JobMode, PasteTarget
from services.focus_context import AppIdentity, FocusSnapshot, TextContext, catalog
from services.focus_context._capture import CaptureService
from services.settings import SettingsKey
from tests.test_personalize_fix_f2_paste_target import CHANGED_STATUS, _run_transform, h  # noqa: F401  (fixture)
from tests.test_personalize_s1_service import FakePlatform
from tests.test_personalize_s3_command_delivery import FakeService


def _chrome(title):
    return AppIdentity(
        "chrome.exe", catalog.app_name("chrome.exe"), pid=4242, window="0x1a2b",
        title_hint=catalog.title_hint("chrome.exe", title), platform="windows",
    )


GMAIL = _chrome("Inbox (12) - someone@gmail.com - Gmail - Google Chrome")
GMAIL_COMPOSE = _chrome("Compose Mail - someone@gmail.com - Gmail - Google Chrome")
WHATSAPP = _chrome("(3) WhatsApp - Google Chrome")
UNKNOWN_SITE = _chrome("Some recipe blog - Google Chrome")


@pytest.fixture
def service():
    platform = FakePlatform(GMAIL)
    instance = CaptureService(platform, settings=lambda: {}, metrics=lambda **_values: None)
    focus_context.set_service(instance)
    yield platform
    instance.shutdown()


def test_the_titles_name_the_sites():
    assert (GMAIL.title_hint, GMAIL_COMPOSE.title_hint, WHATSAPP.title_hint) == (
        "Gmail", "Gmail", "WhatsApp"
    )
    assert UNKNOWN_SITE.title_hint == ""


@pytest.mark.parametrize("aware", [False, True])
@pytest.mark.parametrize("now, expected", [
    (WHATSAPP, PasteTarget.CHANGED),
    (UNKNOWN_SITE, PasteTarget.CHANGED),
    (GMAIL_COMPOSE, PasteTarget.SAME),
    (GMAIL, PasteTarget.SAME),
])
def test_paste_check_compares_the_site_too(service, aware, now, expected):
    job = dictation_pipeline.begin_job(
        JobMode.TRANSFORM, {SettingsKey.APP_CONTEXT_ENABLED: aware}, selection="secret"
    )
    service.current = now

    assert dictation_pipeline.paste_target(job) == expected
    assert dictation_pipeline.paste_target_ok(job) is (expected == PasteTarget.SAME)


def test_the_paste_check_and_the_learning_reread_agree():
    assert not focus_context.same_target(WHATSAPP, GMAIL)
    assert focus_context.same_target(GMAIL_COMPOSE, GMAIL)
    assert not focus_context.same_target(None, GMAIL)


def test_a_transform_finished_after_a_tab_switch_is_copied_not_pasted(h):  # noqa: F811
    focus_context.set_service(FakeService(
        FocusSnapshot(GMAIL, TextContext(selected="CONFIDENTIAL draft", selection_known=True)),
        current=WHATSAPP))

    _run_transform(h)

    h.paste.assert_not_called()
    assert h.ui.copied == ["CONFIDENTIAL rewrite"]
    assert h.ui.statuses[-1] == CHANGED_STATUS
