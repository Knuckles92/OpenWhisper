import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.local_asr.languages import LANGUAGE_LABELS, language_choices
from services.settings import SettingsKey, SettingsManager, settings_manager
from transcriber.optional_backend import LocalSpeechBackend
from ui_qt.widgets import local_engine_controls as controls


@pytest.fixture
def widget(tmp_path, monkeypatch):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    monkeypatch.setattr(controls, "settings_manager", manager)
    field = controls.LocalEngineControls()
    yield field, manager
    field.close()


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
@pytest.mark.parametrize("backend,expected", [
    ("parakeet", {"en", "ru", "es", "fr", "pt", "auto"}),
    ("qwen_asr", {"en", "ru", "es", "fr", "pt", "zh", "auto"}),
    ("nemotron", {"en", "ru", "es", "fr", "pt", "zh", "auto"}),
    ("moonshine", {"en"}),
])
def test_selector_limits_languages_and_fits_narrow_windows(widget, monkeypatch, mode, backend, expected):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    field, manager = widget
    manager.save_setting(SettingsKey.LOCAL_ASR_LANGUAGE, "ru")
    before = manager.load_all_settings()
    changed = Mock()
    field.engine_settings_changed.connect(changed)
    field.set_backend(backend)
    assert {field.language_combo.itemData(i) for i in range(field.language_combo.count())} == expected
    assert field.language_combo.isEnabled() == (len(expected) > 1)
    assert manager.load_all_settings() == before
    changed.assert_not_called()
    field.resize(520, 90)
    field.show()
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    assert field.language_combo.width() >= 96
    assert field.language_combo.geometry().right() <= field.language_combo.parentWidget().width()


@pytest.mark.parametrize("code", ["en", "ru", "es", "fr", "pt", "zh", "auto"])
def test_language_round_trip_uses_codes_and_emits_one_reload(widget, code):
    field, manager = widget
    manager.save_setting(SettingsKey.LOCAL_ASR_LANGUAGE, "auto" if code != "auto" else "en")
    field.set_backend("qwen_asr")
    changed = Mock()
    field.engine_settings_changed.connect(changed)
    field.language_combo.setCurrentIndex(field.language_combo.findData(code))
    assert manager.get(SettingsKey.LOCAL_ASR_LANGUAGE) == code
    changed.assert_called_once_with()
    field.load_from_settings()
    assert field.language_combo.currentData() == code
    assert field.language_combo.currentText() == LANGUAGE_LABELS[code]
    changed.assert_called_once_with()


def test_moonshine_model_change_cannot_preserve_russian(widget):
    field, manager = widget
    field.set_backend("parakeet")
    field.language_combo.setCurrentText("Russian")
    field.set_backend("moonshine")
    assert field.language_combo.currentText() == "English"
    field.model_combo.setCurrentIndex(field.model_combo.findData("moonshine-medium"))
    assert manager.get(SettingsKey.LOCAL_ASR_LANGUAGE) == "en"
    field.set_busy(True)
    field.set_busy(False)
    assert not field.language_combo.isEnabled()


def test_switching_mandarin_to_parakeet_selects_auto_without_saving(widget):
    field, manager = widget
    field.set_backend("qwen_asr")
    field.language_combo.setCurrentText("Mandarin")
    field.set_backend("parakeet")
    assert field.language_combo.currentData() == "auto"
    assert manager.get(SettingsKey.LOCAL_ASR_LANGUAGE) == "zh"


@pytest.mark.parametrize("backend,stored,expected", [
    ("moonshine", "ru", "en"), ("moonshine", "auto", "en"),
    ("parakeet", "zh", "auto"), ("qwen_asr", "zh", "zh"),
    ("nemotron", "ru", "ru"),
])
def test_requests_use_a_supported_language_even_before_controls_open(monkeypatch, backend, stored, expected):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {"local_asr_language": stored}))
    assert LocalSpeechBackend(backend).request_language() == expected


def test_remote_choices_validate_language_before_persisting(monkeypatch):
    from services import components, gpu_info
    from services.remote_asr.runtime import runtime_state, validate_runtime
    monkeypatch.setattr(components, "is_installed", lambda _: True)
    monkeypatch.setattr(gpu_info, "nvidia_gpu", lambda: None)
    for family, model in [("parakeet", "parakeet-v3"), ("qwen_asr", "qwen-0.6b"),
                          ("nemotron", "nemotron-3.5"), ("moonshine", "moonshine-small")]:
        engine = {"family": family, "model": model}
        settings_manager.save_setting(SettingsKey.LOCAL_ASR_LANGUAGE, "zh")
        state = runtime_state(engine)
        assert state["languages"] == list(language_choices(family))
        assert state["selected"]["language"] in state["languages"]
        before = settings_manager.load_all_settings()
        for code in state["languages"]:
            updates = validate_runtime(engine, family, model, {"device": "cpu", "language": code})
            assert updates[SettingsKey.LOCAL_ASR_LANGUAGE] == code
        for code in {"xx", "zh", "ru"} - set(state["languages"]):
            with pytest.raises(ValueError, match="language"):
                validate_runtime(engine, family, model, {"device": "cpu", "language": code})
        assert settings_manager.load_all_settings() == before


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
def test_remote_mandarin_uses_label_and_transmits_code(monkeypatch, mode):
    from ui_qt.widgets.remote_engine_controls import RemoteEngineControls
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    field = RemoteEngineControls()
    changed = Mock()
    field.runtime_selected.connect(changed)
    field.set_state(SimpleNamespace(host="test", engine={"family": "qwen_asr", "model": "qwen-0.6b"},
        runtime={"family": "qwen_asr", "model": "qwen-0.6b", "can_configure": True, "selected": {"device": "cpu", "language": "en"},
                 "devices": ["cpu"], "languages": list(language_choices("qwen_asr"))}))
    index = field.language_combo.findData("zh")
    assert field.language_combo.itemText(index) == "Mandarin"
    field.language_combo.activated.emit(index)
    changed.assert_called_once_with("qwen_asr", "qwen-0.6b", {"language": "zh"})
    field.close()


def run_worker(bootstrap, requests):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", bootstrap], cwd=root,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=20, env={**os.environ, "HF_HUB_OFFLINE": "1"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    return {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}


def test_worker_passes_qwen_language_names_to_sdk():
    bootstrap = """
import runpy, sys, types
class Qwen:
    def __init__(self):
        self.model = types.SimpleNamespace(generate=lambda *a, **k: None)
    @classmethod
    def from_pretrained(cls, *a, **k): return cls()
    def transcribe(self, audio, language):
        assert language is None or language in {'English','Russian','Spanish','French','Portuguese','Chinese','German','Japanese'}, language
        return [types.SimpleNamespace(text=language or 'Auto')]
sys.modules['qwen_asr'] = types.SimpleNamespace(Qwen3ASRModel=Qwen)
sys.modules['torch'] = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda:False), float16='f16', float32='f32')
runpy.run_path('services/local_asr/worker.py', run_name='__main__')
"""
    languages = [('en','English'), ('ru','Russian'), ('es','Spanish'), ('fr','French'),
                 ('pt','Portuguese'), ('zh','Chinese'), ('auto','Auto'), ('en-US','English'),
                 ('pt-BR','Portuguese'), ('zh_CN','Chinese'), ('de','German'), ('ja','Japanese')]
    requests = [dict(id=0, op='load', backend='qwen_asr', device='cpu', model_path='fixture')]
    requests += [dict(id=i, op='transcribe', language=code) for i,(code,_) in enumerate(languages,1)]
    requests += [dict(id=99, op='transcribe', language='uk'), dict(id=100, op='shutdown')]
    responses = run_worker(bootstrap, requests)
    for i, (_, name) in enumerate(languages,1):
        assert responses[i].get('result', {}).get('text') == name, responses[i]
    assert 'does not support' in responses[99]['error']


def test_worker_uses_nemotron_mandarin_prompt_for_file_and_stream():
    bootstrap = """
import runpy, sys, types
class Nvidia:
    def __init__(self, *a): pass
    def transcribe(self, audio, language): return {'text':language}
    def stream(self, session, audio, language, finish): return [{'text':language, 'final':finish}]
sys.modules['services.local_asr.nvidia'] = types.SimpleNamespace(NvidiaRecognizer=Nvidia)
runpy.run_path('services/local_asr/worker.py', run_name='__main__')
"""
    responses = run_worker(bootstrap, [
        dict(id=0, op='load', backend='nemotron', device='cpu', runtime='fixture', model_path='fixture'),
        dict(id=1, op='transcribe', language='zh'),
        dict(id=2, op='stream', language='zh', session='mic', finish=True),
        dict(id=3, op='transcribe', language='ru'), dict(id=4, op='shutdown')])
    assert responses[1]['result']['text'] == 'zh-CN'
    assert responses[2]['result']['events'] == [{'text':'zh-CN','final':True}]
    assert responses[3]['result']['text'] == 'ru'
