"""Text filters against a real HTTP server on 127.0.0.1 and real filter programs."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from services import settings as settings_module
from services.automation import events, filters
from services.automation.models import AFTER_CLEANUP, BEFORE_CLEANUP
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import AppIdentity, FocusSnapshot
from services.recognition_context import RecognitionContext
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_hooks_webhooks import Answer, Receiver, closed_port

PY = sys.executable
TEXT = "please send the report to Ana"
UPPER_PROGRAM = (
    "import json, sys; r = json.loads(sys.stdin.buffer.read()); "
    "sys.stdout.buffer.write(json.dumps({'text': r['text'].upper()}).encode())"
)


def reply_text(transform):
    """An Answer body that rewrites the request's text."""
    def reply(request):
        return json.dumps({"text": transform(request.json()["text"])}).encode()
    return reply


def job(mode: str = JobMode.DICTATION, *, action_id: str = "", app: bool = True) -> DictationJob:
    focus = None
    if app:
        focus = Future()
        focus.set_result(FocusSnapshot(identity=AppIdentity("slack", "Slack")))
    return DictationJob(mode=mode, focus=focus, action_id=action_id,
                        recognition=RecognitionContext(language="en"))


def http_filter(filter_id: str, url: str, stage: str = BEFORE_CLEANUP, **extra) -> dict:
    return {"id": filter_id, "name": f"Filter {filter_id}", "kind": "http", "stage": stage,
            "url": url, **extra}


def program_filter(filter_id: str, code: str, stage: str = BEFORE_CLEANUP, **extra) -> dict:
    return {"id": filter_id, "name": f"Filter {filter_id}", "kind": "program", "stage": stage,
            "program": PY, "args": ["-c", code], **extra}


def settings_with(*rows: dict, leases_allowed: bool = False) -> dict:
    return {SettingsKey.AUTOMATION_FILTERS: list(rows),
            SettingsKey.API_TEXT_FILTERS: leases_allowed}


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


@pytest.fixture
def failures():
    """filter.failed events published during the test."""
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    got: list = []
    fresh.subscribe(got.append, types=["filter.failed"])
    yield got
    fresh.close()
    events.set_bus(previous)


@pytest.fixture(autouse=True)
def no_leases():
    filters.drop_all_leases()
    yield
    filters.drop_all_leases()


def wait_for(items: list, count: int, timeout: float = 5.0) -> list:
    deadline = time.monotonic() + timeout
    while len(items) < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"expected {count}, got {len(items)}")
        time.sleep(0.01)
    return items


# -- when filters apply ---------------------------------------------------------------

def test_nothing_configured_returns_the_text_unchanged():
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings_with())
    assert outcome == filters.FilterOutcome(TEXT)


@pytest.mark.parametrize("the_job", [
    None, job(JobMode.COMMAND), job(JobMode.TRANSFORM),
], ids=["no job", "command", "transform"])
def test_only_dictation_and_action_jobs_are_filtered(receiver, the_job):
    settings = settings_with(http_filter("f", receiver.url("/f")))
    assert filters.apply(BEFORE_CLEANUP, TEXT, the_job, settings) == filters.FilterOutcome(TEXT)
    time.sleep(0.1)
    assert receiver.requests == []


@pytest.mark.parametrize("silence", ["", "   "])
def test_no_words_are_never_sent_to_a_filter(receiver, silence):
    settings = settings_with(http_filter("f", receiver.url("/f")))
    assert filters.apply(BEFORE_CLEANUP, silence, job(), settings) == filters.FilterOutcome(silence)
    assert receiver.requests == []


def test_an_http_filter_gets_the_contract_request_and_replaces_the_text(receiver):
    receiver.answer("/f", Answer(reply=reply_text(str.upper)))
    the_job = job(JobMode.ACTION, action_id="claude")
    outcome = filters.apply(AFTER_CLEANUP, TEXT, the_job,
                            settings_with(http_filter("f", receiver.url("/f"), AFTER_CLEANUP)))

    assert outcome == filters.FilterOutcome(TEXT.upper(), canceled=False, changed=True)
    request = receiver.requests[0]
    assert request.headers["content-type"] == "application/json"
    assert request.json() == {
        "v": 1, "stage": "after_cleanup", "job_id": the_job.id, "mode": "action",
        "action_id": "claude", "text": TEXT, "language": "en",
        "app": {"id": "slack", "name": "Slack", "category": "work"},
    }


def test_unknown_app_and_language_are_null(receiver):
    receiver.answer("/f", Answer(body=b"{}"))
    bare = DictationJob(mode=JobMode.DICTATION)
    filters.apply(BEFORE_CLEANUP, TEXT, bare, settings_with(http_filter("f", receiver.url("/f"))))
    body = receiver.requests[0].json()
    assert (body["app"], body["language"], body["action_id"]) == (None, None, None)


def test_saved_filters_run_in_order_then_leases(receiver):
    receiver.answer("/a", Answer(reply=reply_text(lambda t: t + " A")))
    receiver.answer("/b", Answer(reply=reply_text(lambda t: t + " B")))
    receiver.answer("/lease", Answer(reply=reply_text(lambda t: t + " L")))
    filters.register_lease(receiver.url("/lease"), BEFORE_CLEANUP)
    rows = (http_filter("a", receiver.url("/a")), http_filter("b", receiver.url("/b")))

    allowed = filters.apply(BEFORE_CLEANUP, "x", job(), settings_with(*rows, leases_allowed=True))
    assert allowed.text == "x A B L"
    # Leases need the text filters permission, checked on every dictation.
    refused = filters.apply(BEFORE_CLEANUP, "x", job(), settings_with(*rows))
    assert refused.text == "x A B"


def test_only_enabled_filters_for_the_stage_run(receiver):
    receiver.answer("/f", Answer(reply=reply_text(str.upper)))
    settings = settings_with(
        http_filter("after", receiver.url("/f"), AFTER_CLEANUP),
        http_filter("off", receiver.url("/f"), enabled=False),
    )
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(), settings).text == TEXT
    assert receiver.requests == []
    assert filters.apply(AFTER_CLEANUP, TEXT, job(), settings).text == TEXT.upper()


def test_lease_modes_choose_the_jobs(receiver):
    receiver.answer("/lease", Answer(reply=reply_text(str.upper)))
    filters.register_lease(receiver.url("/lease"), BEFORE_CLEANUP, modes=["action"])
    settings = settings_with(leases_allowed=True)
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(), settings).text == TEXT
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(JobMode.ACTION), settings).text == TEXT.upper()


def test_settings_are_read_when_not_given(receiver, monkeypatch):
    receiver.answer("/f", Answer(reply=reply_text(str.upper)))
    monkeypatch.setattr(settings_module, "settings_manager",
                        InMemorySettings(settings_with(http_filter("f", receiver.url("/f")))))
    assert filters.apply(BEFORE_CLEANUP, TEXT, job()).text == TEXT.upper()


# -- replies ------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    Answer(200, body=b"{}"),
    Answer(204),
    Answer(200, body=b""),
    Answer(200, body=b'{"cancel": false, "note": "ignored"}'),
], ids=["empty object", "204", "empty body", "no cancel"])
def test_replies_that_keep_the_text(receiver, failures, answer):
    receiver.answer("/f", answer)
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(http_filter("f", receiver.url("/f"))))
    assert outcome == filters.FilterOutcome(TEXT)
    time.sleep(0.1)
    assert failures == []


@pytest.mark.parametrize("body", [b'{"cancel": true}', b'{"text": ""}', b'{"text": "  "}'])
def test_a_cancel_stops_the_chain_and_delivers_nothing(receiver, body):
    receiver.answer("/cancel", Answer(body=body))
    receiver.answer("/later", Answer(reply=reply_text(str.upper)))
    settings = settings_with(http_filter("c", receiver.url("/cancel")),
                             http_filter("later", receiver.url("/later")))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings)
    assert outcome.canceled
    assert receiver.at("/later") == []


@pytest.mark.parametrize(("answer", "reason"), [
    (Answer(500, body=b'{"text": "no"}'), "HTTP 500"),
    (Answer(404), "HTTP 404"),
    (Answer(302, {"Location": "/elsewhere"}), "HTTP 302: redirects aren't followed"),
    (Answer(body=b"rewritten, but not JSON"), "The reply wasn't JSON"),
    (Answer(body=b'["a list"]'), "The reply wasn't a JSON object"),
    (Answer(body=b'{"text": 5}'), '"text" wasn\'t a string'),
    (Answer(body=b'{"text": null}'), '"text" wasn\'t a string'),
    (Answer(body=b'{"cancel": "yes"}'), '"cancel" wasn\'t true or false'),
], ids=["500", "404", "redirect", "not json", "list", "number", "null", "cancel string"])
def test_a_bad_answer_keeps_the_text_and_says_why(receiver, failures, answer, reason):
    receiver.answer("/f", answer)
    receiver.answer("/next", Answer(reply=reply_text(str.upper)))
    settings = settings_with(http_filter("f", receiver.url("/f")),
                             http_filter("next", receiver.url("/next")))
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings)

    # The text is kept and the next filter still runs.
    assert outcome == filters.FilterOutcome(TEXT.upper(), changed=True)
    assert receiver.at("/elsewhere") == []
    event = wait_for(failures, 1)[0]
    assert event.data == {"filter_id": "f", "name": "Filter f", "stage": "before_cleanup",
                          "reason": reason}
    assert TEXT not in event.to_json()


def test_an_unreachable_filter_keeps_the_text(failures):
    settings = settings_with(http_filter("f", f"http://127.0.0.1:{closed_port()}/f",
                                         timeout_ms=5000))
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(), settings) == filters.FilterOutcome(TEXT)
    assert wait_for(failures, 1)[0].data["reason"] in ("Couldn't connect", "No answer within 5 s")


# -- time limits -------------------------------------------------------------------------

def test_a_slow_filter_times_out_to_the_original_text(receiver, failures):
    receiver.answer("/slow", Answer(delay=5, reply=reply_text(str.upper)))
    settings = settings_with(http_filter("slow", receiver.url("/slow"), timeout_ms=300))
    started = time.monotonic()
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings)
    elapsed = time.monotonic() - started

    assert outcome == filters.FilterOutcome(TEXT)
    assert 0.29 <= elapsed < 1.0
    assert wait_for(failures, 1)[0].data["reason"] == "No answer within 0.3 s"


def test_setting_up_requests_is_not_charged_to_a_filters_time(receiver, failures, monkeypatch):
    from services.automation import webhooks

    real = webhooks._new_client

    def slow_first_client(private):
        time.sleep(0.5)  # importing httpx and building TLS on a cold start
        return real(private)

    monkeypatch.setattr(filters, "_clients", {})
    monkeypatch.setattr(webhooks, "_new_client", slow_first_client)
    receiver.answer("/f", Answer(reply=reply_text(str.upper)))
    settings = settings_with(http_filter("f", receiver.url("/f"), timeout_ms=200))
    assert filters.apply(BEFORE_CLEANUP, TEXT, job(), settings).text == TEXT.upper()
    assert failures == []


def test_registering_a_lease_prepares_requests_in_the_background(monkeypatch):
    monkeypatch.setattr(filters, "_clients", {})
    started = time.monotonic()
    filters.register_lease("http://127.0.0.1:9/f", BEFORE_CLEANUP)
    assert time.monotonic() - started < 0.1
    wait_for_true = time.monotonic() + 5
    while True not in filters._clients and time.monotonic() < wait_for_true:
        time.sleep(0.01)
    assert True in filters._clients


def test_the_stage_budget_holds_and_skips_what_is_left(receiver, failures):
    receiver.answer("/hang", Answer(delay=10))
    settings = settings_with(*(http_filter(name, receiver.url("/hang"), timeout_ms=2000)
                               for name in ("one", "two", "three")))
    started = time.monotonic()
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings)
    elapsed = time.monotonic() - started

    assert outcome == filters.FilterOutcome(TEXT)
    assert 2.9 <= elapsed < 3.4
    reasons = [e.data["reason"] for e in wait_for(failures, 3)]
    assert reasons[0] == "No answer within 2 s"
    # The second filter only had what was left of the stage's 3 s.
    assert reasons[1].startswith("No answer within ")
    assert 0.85 <= float(reasons[1].split()[3]) <= 1.0
    assert reasons[2] == "Skipped: this stage's 3 s for filters ran out"
    assert [e.data["filter_id"] for e in failures] == ["one", "two", "three"]


# -- program filters ---------------------------------------------------------------------

def test_a_program_filter_reads_stdin_and_answers_on_stdout():
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(program_filter("p", UPPER_PROGRAM)))
    assert outcome == filters.FilterOutcome(TEXT.upper(), changed=True)


def test_a_program_filter_sees_the_whole_request():
    echo = ("import sys, json; r = json.loads(sys.stdin.buffer.read()); "
            "sys.stdout.write(json.dumps({'text': json.dumps(r, sort_keys=True)}))")
    the_job = job()
    outcome = filters.apply(BEFORE_CLEANUP, "héllo", the_job,
                            settings_with(program_filter("p", echo)))
    assert json.loads(outcome.text) == {
        "v": 1, "stage": "before_cleanup", "job_id": the_job.id, "mode": "dictation",
        "action_id": None, "text": "héllo", "language": "en",
        "app": {"id": "slack", "name": "Slack", "category": "work"},
    }


@pytest.mark.parametrize(("code", "reason"), [
    ("import sys; sys.exit(4)", "Exited with code 4"),
    ("print('not json')", "The reply wasn't JSON"),
], ids=["exit code", "not json"])
def test_a_failing_program_filter_keeps_the_text(failures, code, reason):
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings_with(program_filter("p", code)))
    assert outcome == filters.FilterOutcome(TEXT)
    assert wait_for(failures, 1)[0].data["reason"] == reason


def test_a_program_filter_with_no_output_keeps_the_text(failures):
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(),
                            settings_with(program_filter("p", "pass")))
    assert outcome == filters.FilterOutcome(TEXT)
    time.sleep(0.1)
    assert failures == []


def test_a_program_filter_can_cancel():
    cancel = "import sys; sys.stdin.read(); print('{\"cancel\": true}')"
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings_with(program_filter("p", cancel)))
    assert outcome.canceled


def test_a_hanging_program_filter_is_stopped_at_its_limit(failures):
    settings = settings_with(program_filter("p", "import time; time.sleep(30)", timeout_ms=500))
    started = time.monotonic()
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings)
    assert outcome == filters.FilterOutcome(TEXT)
    assert time.monotonic() - started < 2.5
    assert wait_for(failures, 1)[0].data["reason"] == "Timed out after 0.5 s"


# -- leases ----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8123/filter?x=1", "http://localhost:9/f", "http://[::1]:9/f",
    "https://LOCALHOST/f",
])
def test_leases_accept_loopback_addresses(url):
    lease = filters.register_lease(url, AFTER_CLEANUP, timeout_ms=800, modes=("dictation",))
    assert lease.id.startswith("flt_")
    assert (lease.url, lease.stage, lease.timeout_ms, lease.modes) == (
        url, "after_cleanup", 800, ("dictation",))
    assert lease.expires_at == pytest.approx(time.time() + filters.LEASE_TTL_S, abs=2)
    assert filters.leases() == [lease]


@pytest.mark.parametrize("url", [
    "http://192.168.1.10/f", "http://example.com/f", "http://127.0.0.2/f",
    "http://localhost.example.com/f", "ftp://127.0.0.1/f", "127.0.0.1:9/f", "",
])
def test_leases_refuse_other_addresses(url):
    with pytest.raises(ValueError):
        filters.register_lease(url, BEFORE_CLEANUP)
    assert filters.leases() == []


@pytest.mark.parametrize("kwargs", [
    {"stage": "during_cleanup"},
    {"timeout_ms": 50}, {"timeout_ms": 6000}, {"timeout_ms": True}, {"timeout_ms": "1500"},
    {"modes": "dictation"}, {"modes": ["command"]}, {"modes": []}, {"modes": ["file"]},
])
def test_leases_validate_their_settings(kwargs):
    stage = kwargs.pop("stage", BEFORE_CLEANUP)
    with pytest.raises(ValueError):
        filters.register_lease("http://127.0.0.1:9/f", stage, **kwargs)


def test_at_most_ten_leases_even_when_registered_at_once():
    results: list = []

    def register():
        try:
            results.append(filters.register_lease("http://127.0.0.1:9/f", BEFORE_CLEANUP))
        except ValueError as exc:
            results.append(exc)

    threads = [threading.Thread(target=register) for _ in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    leases = [r for r in results if isinstance(r, filters.Lease)]
    assert len(leases) == filters.MAX_LEASES
    assert len({lease.id for lease in leases}) == filters.MAX_LEASES
    assert len(filters.leases()) == filters.MAX_LEASES


def test_leases_expire_unless_renewed(monkeypatch):
    monkeypatch.setattr(filters, "LEASE_TTL_S", 0.3)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(filters, "time", SimpleNamespace(
        time=lambda: clock.now, monotonic=lambda: clock.now))
    kept = filters.register_lease("http://127.0.0.1:9/kept", BEFORE_CLEANUP)
    lapsed = filters.register_lease("http://127.0.0.1:9/lapsed", BEFORE_CLEANUP)
    clock.now += 0.2
    renewed = filters.renew_lease(kept.id)
    assert renewed.expires_at > kept.expires_at
    clock.now += 0.2
    assert filters.leases() == [renewed]
    with pytest.raises(KeyError):
        filters.renew_lease(lapsed.id)
    with pytest.raises(KeyError):
        filters.renew_lease("flt_unknown")


def test_drop_lease():
    lease = filters.register_lease("http://127.0.0.1:9/f", BEFORE_CLEANUP)
    assert filters.drop_lease(lease.id) is True
    assert filters.drop_lease(lease.id) is False
    assert filters.leases() == []
    filters.register_lease("http://127.0.0.1:9/f", BEFORE_CLEANUP)
    assert filters.drop_all_leases() == 1


def test_a_failing_lease_is_reported_by_id(failures):
    lease = filters.register_lease(f"http://127.0.0.1:{closed_port()}/f", BEFORE_CLEANUP,
                                   timeout_ms=200)
    outcome = filters.apply(BEFORE_CLEANUP, TEXT, job(), settings_with(leases_allowed=True))
    assert outcome == filters.FilterOutcome(TEXT)
    data = wait_for(failures, 1)[0].data
    assert (data["filter_id"], data["name"]) == (lease.id, "API filter")


# -- privacy ---------------------------------------------------------------------------------

def test_logs_never_carry_the_text_or_the_address(receiver, failures, caplog):
    caplog.set_level(logging.DEBUG)
    receiver.answer("/f?key=sekrit", Answer(500))
    filters.apply(BEFORE_CLEANUP, TEXT, job(),
                  settings_with(http_filter("f", receiver.url("/f?key=sekrit")),
                                program_filter("p", "import sys; sys.exit(1)")))
    wait_for(failures, 2)
    for needle in (TEXT, "sekrit", f":{receiver.port}"):
        assert needle not in caplog.text
