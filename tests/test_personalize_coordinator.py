"""Cross-stream wiring added when the personalization streams were merged."""

import importlib

import pytest

from services.history_export import _cleanup_label
from services.settings import SettingsKey


@pytest.mark.parametrize("settings, expected", [
    ({}, False),
    ({SettingsKey.HOTKEYS: {"command_mode": "ctrl+alt+k"}}, True),
    ({SettingsKey.HOTKEYS: {"command_mode": ""}}, False),
    ({SettingsKey.TEXT_TRANSFORMS: [
        {"id": "p", "name": "Polish", "instruction": "Polish it.", "hotkey": "ctrl+alt+p"},
    ]}, True),
    ({SettingsKey.TEXT_TRANSFORMS: [
        {"id": "p", "name": "Polish", "instruction": "Polish it.", "hotkey": ""},
    ]}, False),
])
def test_rewrite_shortcuts_warm_the_cleanup_sdk(settings, expected):
    cleanup_profiles = importlib.import_module("services.cleanup_profiles")
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_PROFILES: [], **settings}
    assert cleanup_profiles.cleanup_may_run(settings) is expected


def test_export_says_when_the_original_was_kept():
    entry = {"text": "um hi", "raw_text": "um hi", "cleaned_text": "Hi.",
             "cleanup_provider": "openrouter", "cleanup_model": "m"}
    assert _cleanup_label(entry) == "openrouter · m (original kept)"
    entry["text"] = "Hi."
    assert _cleanup_label(entry) == "openrouter · m"


def test_copy_only_delivery_keeps_snippet_formatting(monkeypatch):
    transcription = importlib.import_module("services.runtime.transcription")
    copies = []

    class UI:
        def discard_clipboard_prefetch(self):
            pass

        def copy_to_clipboard(self, text, html=""):
            copies.append((text, html))
            return True

        def set_status(self, text):
            pass

    class Settings:
        def load_all_settings(self):
            return {SettingsKey.COPY_CLIPBOARD: True, SettingsKey.AUTO_PASTE: False}

    runtime = transcription.TranscriptionRuntime.__new__(transcription.TranscriptionRuntime)
    runtime.controller = type("C", (), {"ui_controller": UI()})()
    monkeypatch.setattr(transcription, "settings_manager", Settings())
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    runtime._apply_clipboard_and_paste("Book here: cal.example", html="<a>Book</a>")
    runtime._apply_clipboard_and_paste("plain")
    assert copies == [("Book here: cal.example", "<a>Book</a>"), ("plain", "")]
