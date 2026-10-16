"""The API backend's Server and Model fields."""
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from services import speech_endpoints
from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.speech_server_dialog import SpeechServerDialog
from ui_qt.widgets import speech_server_controls
from ui_qt.widgets.speech_server_controls import SpeechServerControls


@pytest.fixture(autouse=True)
def fresh_catalogs(monkeypatch):
    monkeypatch.setattr(speech_server_controls, "_catalogs", {})


@pytest.fixture
def catalog(monkeypatch):
    """Answer catalog requests with ``catalog.models`` once ``catalog.release`` is set."""
    class Catalog:
        models = ["Systran/faster-whisper-large-v3", "Systran/faster-whisper-small"]
        error = None
        release = threading.Event()
        asked = []

    Catalog.release.set()
    real = speech_endpoints.list_models

    def list_models(endpoint, timeout=5.0):
        if endpoint.is_openai or endpoint.models_path is None:
            return real(endpoint)
        Catalog.asked.append(endpoint.id)
        Catalog.release.wait(5)
        if Catalog.error:
            raise Catalog.error
        return list(Catalog.models)

    monkeypatch.setattr(speech_endpoints, "list_models", list_models)
    return Catalog


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QApplication.processEvents()
        time.sleep(0.01)


def items(combo):
    return [combo.itemData(i) for i in range(combo.count())]


def choose(combo, data):
    combo.setCurrentIndex(combo.findData(data))


def test_openai_is_shown_with_its_models():
    controls = SpeechServerControls()
    assert controls.server_combo.currentData() == "openai"
    assert items(controls.server_combo)[:7] == list(speech_endpoints.ENDPOINT_IDS)
    assert controls.server_combo.itemText(controls.server_combo.findData("custom")) == "Custom…"
    assert controls.model_combo.currentData() == "gpt-transcribe"
    assert "whisper-1 (retiring" in controls.model_combo.itemText(controls.model_combo.findData("whisper-1"))
    edit = controls.server_combo.model().item(controls.server_combo.findData("__edit__"))
    assert not edit.isEnabled()
    assert "uploaded to OpenAI (api.openai.com)" in controls.server_combo.toolTip()


def test_picking_a_local_server_saves_it_and_lists_its_models(catalog):
    controls = SpeechServerControls()
    changes = []
    controls.changed.connect(lambda: changes.append(True))
    choose(controls.server_combo, "speaches")
    assert settings_manager.get(SettingsKey.API_TRANSCRIPTION_ENDPOINT) == "speaches"
    assert changes == [True]
    # The preset's default model stays listed even if the server lacks it.
    wait_until(lambda: controls.model_combo.count() == 2)
    assert items(controls.model_combo) == list(catalog.models)
    assert controls.model_combo.currentData() == "Systran/faster-whisper-small"
    assert catalog.asked == ["speaches"]
    assert "stays on this computer" in controls.server_combo.toolTip()
    choose(controls.model_combo, "Systran/faster-whisper-large-v3")
    assert speech_endpoints.selected_model() == "Systran/faster-whisper-large-v3"
    assert settings_manager.get(SettingsKey.API_TRANSCRIPTION_MODEL) is None
    assert controls.server_combo.model().item(
        controls.server_combo.findData("__edit__")).isEnabled()


def test_a_late_catalog_for_another_server_is_kept_but_not_shown(catalog):
    catalog.release = threading.Event()
    controls = SpeechServerControls()
    choose(controls.server_combo, "speaches")
    choose(controls.server_combo, "groq")
    catalog.release.set()
    wait_until(lambda: speech_server_controls._catalogs)
    QApplication.processEvents()
    assert items(controls.model_combo) == ["whisper-large-v3-turbo", "whisper-large-v3"]


def test_an_unreachable_server_says_so(catalog):
    catalog.error = ConnectionError("refused")
    controls = SpeechServerControls()
    choose(controls.server_combo, "vllm")
    wait_until(lambda: "Couldn't reach vLLM" in controls.model_combo.toolTip())
    assert items(controls.model_combo) == ["openai/whisper-large-v3-turbo"]


def _answer_dialog(monkeypatch, url, model, accept=True):
    def exec_(dialog):
        if not accept:
            return QDialog.DialogCode.Rejected
        dialog.url_edit.setText(url)
        dialog.model_edit.setText(model)
        dialog._save()
        return dialog.result()

    monkeypatch.setattr(SpeechServerDialog, "exec", exec_)


def test_custom_without_an_address_asks_for_one(monkeypatch, catalog):
    controls = SpeechServerControls()
    _answer_dialog(monkeypatch, "", "", accept=False)
    choose(controls.server_combo, "custom")
    assert controls.server_combo.currentData() == "openai"
    assert settings_manager.get(SettingsKey.API_TRANSCRIPTION_ENDPOINT) is None

    _answer_dialog(monkeypatch, "http://10.0.0.5:9000", "large-v3")
    choose(controls.server_combo, "custom")
    assert controls.server_combo.currentData() == "custom"
    assert controls.server_combo.currentText() == "Custom server"
    assert speech_endpoints.selected_endpoint().base_url == "http://10.0.0.5:9000/v1"
    assert speech_endpoints.selected_model() == "large-v3"
    assert "uploaded to Custom server (10.0.0.5:9000)" in controls.server_combo.toolTip()


def test_edit_server_changes_the_address_and_keeps_the_selection(monkeypatch, catalog):
    speech_endpoints.save_selection("whisper_cpp")
    controls = SpeechServerControls()
    _answer_dialog(monkeypatch, "http://127.0.0.1:9090/v1", "ggml-large-v3")
    choose(controls.server_combo, "__edit__")
    assert controls.server_combo.currentData() == "whisper_cpp"
    assert speech_endpoints.selected_endpoint().base_url == "http://127.0.0.1:9090/v1"
    assert controls.model_combo.currentData() == "ggml-large-v3"


def test_dialog_reports_a_bad_address():
    dialog = SpeechServerDialog(speech_endpoints.configured_endpoint("localai"))
    dialog.url_edit.setText("http://user:secret@host/v1")
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "without credentials" in dialog.error_label.text()
