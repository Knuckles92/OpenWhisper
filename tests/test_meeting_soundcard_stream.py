"""Windows/macOS SoundCard loopback: how the recorder thread opens the device."""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import types
from types import SimpleNamespace

from meeting.capture import soundcard_stream
from meeting.capture.soundcard_stream import SoundcardLoopbackSource


def test_loopback_imports_soundcard_before_initializing_com(monkeypatch):
    # SoundCard initializes COM on the thread that first imports it and
    # raises (Error 0x100000001) if that thread already did, so a first open
    # must import before initializing COM.
    events = []
    fake = types.ModuleType("soundcard")
    speaker = SimpleNamespace(name="Speakers", id="speakers-id")
    fake.default_speaker = lambda: speaker
    fake.get_microphone = lambda id, include_loopback: SimpleNamespace(
        name=f"{id} loopback", channels=2,
    )

    class FirstImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            if name != "soundcard":
                return None
            return importlib.util.spec_from_loader(name, self)

        def create_module(self, spec):
            events.append("import")
            return fake

        def exec_module(self, module):
            pass

    # Set then delete, so undoing restores whatever was there before.
    monkeypatch.setitem(sys.modules, "soundcard", fake)
    monkeypatch.delitem(sys.modules, "soundcard")
    monkeypatch.setattr(sys, "meta_path", [FirstImport(), *sys.meta_path])
    monkeypatch.setattr(soundcard_stream, "_coinitialize", lambda: events.append("com"))

    source = SoundcardLoopbackSource()
    sc, monitor, sink_label, _monitor_label, server_kind = source._open_default_loopback()

    assert events == ["import", "com"]
    assert sc is fake
    assert sink_label == "Speakers"
    assert monitor.name == "Speakers loopback"
    assert server_kind == "wasapi"
