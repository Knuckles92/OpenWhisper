import threading
from datetime import date

import pytest

from meeting.finalization import (
    failed_steps_message,
    make_step,
    run_agent_call,
    speaker_pass_gate,
)
from meeting.interfaces import AgentResult


def test_failure_summary_preserves_reason_and_multiple_failed_stages():
    polish = {**make_step("polish", "failed"), "detail": "Timed out after 60s."}
    summary = {**make_step("consolidation", "failed"), "detail": "Provider unavailable."}
    result = failed_steps_message([polish, summary, make_step("finalize", "completed")])
    assert "Transcript Cleanup, Summary & Action Items failed" in result
    assert "Timed out after 60s." in result
    assert "Provider unavailable." in result
    assert "recording and transcript were kept" in result
    assert "State Finalization:" not in result


def test_legacy_failure_without_detail_and_success():
    assert "polish failed" in failed_steps_message([{"id": "polish", "status": "failed"}])
    assert failed_steps_message([make_step("polish", "completed")]) == ""


class TestSpeakerPassGate:
    @pytest.mark.parametrize("backend", ["off", "local", "", "bogus"])
    def test_only_openai_is_offered(self, backend):
        gate = speaker_pass_gate(
            backend=backend, consent=True, find_key=lambda: "sk-test",
        )
        assert (gate.ok, gate.offered, gate.api_key) == (False, False, "")
        assert gate.reason == "Speaker identification is not set to OpenAI."

    def test_retired_model_is_refused_even_with_consent(self):
        gate = speaker_pass_gate(
            backend="openai", consent=True, find_key=lambda: "sk-test",
            today=date(2027, 2, 26),
        )
        assert gate.ok is False
        assert gate.offered is False
        assert "retired" in gate.reason

    def test_consent_is_checked_before_the_key_is_read(self):
        reads = []
        gate = speaker_pass_gate(
            backend="openai", consent=False,
            find_key=lambda: reads.append(True) or "sk-test",
        )
        assert reads == []
        assert (gate.ok, gate.offered) == (False, True)
        assert gate.reason == "Audio-upload consent has not been given."

    @pytest.mark.parametrize("found", ["", None])
    def test_missing_key_is_refused(self, found):
        gate = speaker_pass_gate(
            backend="openai", consent=True, find_key=lambda: found,
        )
        assert (gate.ok, gate.offered) == (False, True)
        assert gate.reason == "No OpenAI API key is configured."

    def test_eligible_gate_carries_the_key_without_printing_it(self):
        gate = speaker_pass_gate(
            backend="openai", consent=True, find_key=lambda: "sk-secret",
        )
        assert (gate.ok, gate.api_key, gate.reason) == (True, "sk-secret", "")
        assert "sk-secret" not in repr(gate)

    def test_injected_decoder_needs_no_key(self):
        gate = speaker_pass_gate(backend="openai", consent=True, find_key=None)
        assert gate.ok is True
        assert gate.api_key == ""


class TestRunAgentCall:
    def test_returns_the_result(self):
        result = run_agent_call(
            lambda: AgentResult(ok=True), cancel=lambda: None,
            timeout_s=5.0, name="test-call",
        )
        assert result.ok is True

    def test_a_raising_call_is_a_failed_result(self):
        def boom():
            raise RuntimeError("sidecar died")

        result = run_agent_call(
            boom, cancel=lambda: None, timeout_s=5.0, name="test-call",
        )
        assert result.ok is False
        assert result.error == "sidecar died"

    def test_timeout_runs_the_handler_before_cancel(self):
        release = threading.Event()
        order = []

        def hang():
            release.wait(timeout=10.0)
            return AgentResult(ok=True)

        def cancel():
            order.append("cancel")
            release.set()

        result = run_agent_call(
            hang, cancel=cancel, timeout_s=0.05, name="test-call",
            on_timeout=lambda: order.append("revoke"),
        )
        assert result is None
        assert order == ["revoke", "cancel"]
