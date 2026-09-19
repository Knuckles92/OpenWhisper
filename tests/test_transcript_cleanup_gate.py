"""The sensitivity gate keeps flagged dictation off remote cleanup models and never blocks cleanup otherwise."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import transcript_cleanup as tc
from services.transcript_cleanup import SENSITIVE_SKIP_REASON, TranscriptCleanup


def cleaner(gate):
    with patch.object(tc, "resolve_typesafe_cleanup_sensitivity_gate", return_value=False):
        instance = TranscriptCleanup(api_key="test-key", sensitivity_gate=gate)
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="Cleaned."))]
    )
    instance.client = client
    return instance, client


def test_flagged_text_stays_raw_and_reports_reason():
    instance, client = cleaner(lambda text: 0.96)
    assert instance.cleanup("my password is hunter two") == "my password is hunter two"
    assert instance.last_error == SENSITIVE_SKIP_REASON
    client.chat.completions.create.assert_not_called()


def test_below_threshold_proceeds():
    instance, client = cleaner(lambda text: 0.2)
    assert instance.cleanup("send the report tomorrow") == "Cleaned."
    assert instance.last_error is None
    client.chat.completions.create.assert_called_once()


def test_no_answer_and_gate_errors_proceed():
    for gate in (lambda text: None, MagicMock(side_effect=TimeoutError("slow"))):
        instance, client = cleaner(gate)
        assert instance.cleanup("hello there") == "Cleaned."
        client.chat.completions.create.assert_called_once()


def test_local_endpoint_is_never_screened():
    gate = MagicMock(return_value=0.99)
    instance, client = cleaner(gate)
    local_profile = SimpleNamespace(is_local=True, kind="custom", api_key_env="")
    with patch.object(tc, "get_profile", return_value=local_profile), \
            patch.object(instance, "_request_options", return_value={}):
        assert instance.cleanup("my password is hunter two") == "Cleaned."
    gate.assert_not_called()
    client.chat.completions.create.assert_called_once()


def test_default_gate_absent_when_setting_off():
    with patch.object(tc, "resolve_typesafe_cleanup_sensitivity_gate", return_value=False):
        assert TranscriptCleanup(api_key="test-key").sensitivity_gate is None


def test_default_gate_built_from_typesafe_when_enabled():
    judge = MagicMock()
    judge.noul.return_value = 0.9
    with patch.object(tc, "resolve_typesafe_cleanup_sensitivity_gate", return_value=True), \
            patch("services.typesafe.judge_from_settings", return_value=judge):
        instance = TranscriptCleanup(api_key="test-key")
    assert instance.sensitivity_gate is not None
    assert instance.sensitivity_gate("card number four one one one") == 0.9
    state = judge.noul.call_args.args[0]
    assert state == {"text": "card number four one one one"}


def test_default_gate_absent_without_key():
    with patch.object(tc, "resolve_typesafe_cleanup_sensitivity_gate", return_value=True), \
            patch("services.typesafe.judge_from_settings", return_value=None):
        assert TranscriptCleanup(api_key="test-key").sensitivity_gate is None
