"""The real private RPC process must start and shut down without a GUI toolkit."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = """
import sys
class NoGui:
    def find_spec(self, fullname, *_args):
        if fullname.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('Forbidden GUI import: ' + fullname)
sys.meta_path.insert(0, NoGui())
from services.settings import settings_manager
settings_manager.save_model_selection('api')
from services.native_desktop.commands import _COMMANDS
def prompt(context, params):
    return context.ui.request_prompt('test', 'Continue?', 'A real service prompt',
        [{'label': 'Continue', 'value': True}, {'label': 'Cancel', 'value': False}],
        default=False)
_COMMANDS['_test.prompt'] = prompt
from services.native_desktop.bootstrap import main
code = main()
assert not any(n.startswith(('PyQt6', 'ui_qt')) for n in sys.modules)
raise SystemExit(code)
"""


class NativeProcess:
    def __init__(self, folder, log_path, *, source=SOURCE, environment=None):
        self.errors = log_path.open("w+")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", source], cwd=ROOT,
            env={**os.environ, "OPENWHISPER_DESKTOP_RUNTIME": "native",
                 "OPENWHISPER_DATA_DIR": str(folder), "HF_HUB_OFFLINE": "1", **(environment or {})},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
            text=True,
        )
        self.lines = queue.Queue()
        self.pending = []
        self.raw_lines = []

        def read():
            for line in self.process.stdout:
                self.raw_lines.append(line)
                self.lines.put(line)
            self.lines.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

    def wait_for(self, predicate, timeout=10):
        for index, packet in enumerate(self.pending):
            if predicate(packet):
                return self.pending.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            assert line is not None, self.diagnostics()
            packet = json.loads(line)
            if predicate(packet):
                return packet
            self.pending.append(packet)
        raise AssertionError("Native service response timed out.\n" + self.diagnostics())

    def ready(self):
        return self.wait_for(lambda p: p.get("event") == "lifecycle" and p["data"]["state"] == "ready")

    def response(self, request_id):
        return self.wait_for(lambda p: p.get("id") == request_id)

    def diagnostics(self):
        self.errors.flush()
        self.errors.seek(0)
        return self.errors.read()[-8000:]

    def exited(self):
        assert self.process.wait(timeout=10) == 0, self.diagnostics()
        self.reader.join(timeout=1)
        assert "Fatal Python error" not in self.diagnostics()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self.reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.errors):
            if stream is not None:
                stream.close()


@pytest.fixture
def start_native(tmp_path):
    processes = []

    def start(folder=None):
        process = NativeProcess(folder or tmp_path / "data", tmp_path / f"service-{len(processes)}.log")
        processes.append(process)
        return process

    yield start
    for process in processes:
        process.close()


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_no_stderr_preserves_protocol_pipe_and_ascii_safe_unicode(tmp_path, frozen):
    prefix = """
sys.stderr = None
from services.native_desktop.bootstrap import reserve_output
protocol = sys.stdout
assert reserve_output() is protocol
assert reserve_output() is protocol
assert sys.stderr is not None and sys.stdout is sys.stderr
assert sys.stderr.writable()
print('日本語 — Unicode library output 🙂')
"""
    if frozen:
        prefix += f"sys.frozen = True\nsys._MEIPASS = {str(ROOT)!r}\n"
    source = SOURCE.replace("from services.settings import settings_manager", prefix + "\nfrom services.settings import settings_manager")
    source = source.replace("_COMMANDS['_test.prompt'] = prompt", """
_COMMANDS['_test.prompt'] = prompt
def unicode_result(context, params):
    print('Library diagnostics — café 日本語 🙂')
    context.emit('test.unicode', {'text': 'café 日本語 🙂'})
    return {'text': 'café 日本語 🙂'}
_COMMANDS['_test.unicode'] = unicode_result
""")
    native = NativeProcess(tmp_path / "data", tmp_path / "stderr.log", source=source,
        environment={"PYTHONIOENCODING": "ascii:strict"})
    try:
        native.ready()
        native.send({"id": 1, "method": "_test.unicode"})
        assert native.response(1)["result"] == {"text": "café 日本語 🙂"}
        assert native.wait_for(lambda value: value.get("event") == "test.unicode")["data"]["text"] == "café 日本語 🙂"
        native.send({"id": 2, "method": "app.quit"})
        assert native.response(2)["result"] == {"ok": True}
        native.exited()
        assert native.raw_lines and all(line.isascii() for line in native.raw_lines)
    finally:
        native.close()


def test_scratchpad_update_survives_immediate_quit_and_service_relaunch(start_native, tmp_path):
    folder = tmp_path / "durable-notes"
    original = "Notes from the classic desktop\r\n"
    folder.mkdir()
    notes = folder / "scratchpad.txt"
    notes.write_bytes(original.encode())
    first = start_native(folder)
    first.ready()
    first.send({"id": 1, "method": "scratchpad.get"})
    assert first.response(1)["result"]["text"] == "Notes from the classic desktop\n"
    newest = "Newest scratchpad\n日本語 café 🙂"
    first.send({"id": 2, "method": "scratchpad.update", "params": {"text": newest}})
    assert first.response(2)["result"] == {"ok": True}
    first.send({"id": 3, "method": "app.quit"})
    assert first.response(3)["result"] == {"ok": True}
    first.exited()
    assert notes.read_bytes() == newest.encode("utf-8")
    second = start_native(folder)
    second.ready()
    second.send({"id": 4, "method": "scratchpad.get"})
    assert second.response(4)["result"]["text"] == newest
    second.send({"id": 5, "method": "app.quit"})
    second.response(5)
    second.exited()


def test_idle_snapshots_do_not_repeat_large_scratchpad_notes(start_native, tmp_path):
    folder = tmp_path / "large-notes"
    folder.mkdir()
    text = "日本語 café 🙂\n" * 25000
    (folder / "scratchpad.txt").write_text(text, encoding="utf-8")
    native = start_native(folder)
    native.ready()
    for request_id in range(1, 4):
        native.send({"id": request_id, "method": "snapshot"})
        snapshot = native.response(request_id)["result"]
        assert "scratchpad" not in snapshot
        assert snapshot["scratchpad_save"]["loaded"]
        assert len(json.dumps(snapshot)) < 64 * 1024
    native.send({"id": 4, "method": "scratchpad.get"})
    assert native.response(4)["result"]["text"] == text
    native.send({"id": 5, "method": "app.quit"})
    assert native.response(5)["result"]["ok"]
    native.exited()


def test_quit_rejects_unsaved_notes_until_failed_write_can_be_retried(tmp_path):
    source = SOURCE.replace("_COMMANDS['_test.prompt'] = prompt", """
_COMMANDS['_test.prompt'] = prompt
def fail_notes(context, params):
    def blocked(*_args):
        raise PermissionError('test write failure')
    context.ui._scratchpad._writer = blocked
    return {'ok': True}
def recover_notes(context, params):
    from services.native_desktop.scratchpad import _write_atomically
    context.ui._scratchpad._writer = _write_atomically
    return {'ok': True}
_COMMANDS['_test.notes.fail'] = fail_notes
_COMMANDS['_test.notes.recover'] = recover_notes
""")
    native = NativeProcess(tmp_path / "data", tmp_path / "stderr.log", source=source)
    try:
        native.ready()
        native.send({"id": 1, "method": "_test.notes.fail"})
        native.response(1)
        native.send({"id": 2, "method": "scratchpad.update", "params": {"text": "Keep these unsaved words"}})
        assert native.response(2)["result"]["ok"]
        native.send({"id": 3, "method": "app.quit"})
        assert "could not be saved" in native.response(3)["error"]
        assert native.process.poll() is None
        native.send({"id": 4, "method": "snapshot"})
        snapshot = native.response(4)["result"]
        assert "scratchpad" not in snapshot
        assert snapshot["scratchpad_save"]["dirty"]
        native.send({"id": 7, "method": "scratchpad.get"})
        assert native.response(7)["result"]["text"] == "Keep these unsaved words"
        native.send({"id": 5, "method": "_test.notes.recover"})
        native.response(5)
        native.send({"id": 6, "method": "app.quit"})
        assert native.response(6)["result"]["ok"]
        native.exited()
        assert (tmp_path / "data" / "scratchpad.txt").read_text() == "Keep these unsaved words"
    finally:
        native.close()


def test_unreadable_scratchpad_exposes_load_error_and_preserves_original(start_native, tmp_path):
    folder = tmp_path / "unreadable-notes"
    folder.mkdir()
    original = folder / "scratchpad.txt"
    original.mkdir()
    native = start_native(folder)
    native.ready()
    native.send({"id": 1, "method": "scratchpad.get"})
    notes = native.response(1)["result"]
    assert notes["save"]["loaded"] is False and notes["save"]["error"]
    native.send({"id": 2, "method": "scratchpad.update", "params": {"text": "Don't replace unread notes"}})
    assert "preserved" in native.response(2)["error"]
    native.send({"id": 3, "method": "app.quit"})
    assert native.response(3)["result"]["ok"]
    native.exited()
    assert original.is_dir()


def test_valid_unicode_scratchpad_larger_than_one_megabyte_roundtrips(start_native):
    native = start_native()
    native.ready()
    text = "日本語 🙂\n" * 40_000
    assert len(json.dumps(text).encode("ascii")) > 1_048_576
    native.send({"id": 1, "method": "scratchpad.update", "params": {"text": text}})
    assert native.response(1)["result"]["ok"]
    native.send({"id": 2, "method": "scratchpad.get"})
    result = native.response(2)["result"]
    assert result["text"] == text and result["save"]["loaded"]
    native.send({"id": 3, "method": "app.quit"})
    native.response(3)
    native.exited()


@pytest.mark.parametrize("request_quit", [True, False], ids=["app-quit", "pipe-eof"])
def test_shutdown_cancels_live_write_up_before_joining_its_executor(tmp_path, request_quit):
    source = SOURCE.replace("_COMMANDS['_test.prompt'] = prompt", """
_COMMANDS['_test.prompt'] = prompt
def live_write_up(context, params):
    import threading
    from pathlib import Path
    from config import data_root
    cancel = threading.Event()
    entered = threading.Event()
    context.controller.transcription_runtime._write_up_cancel = cancel
    context.ui.set_walkthrough_writing_up(True)
    def work():
        entered.set()
        if not cancel.wait(5):
            raise RuntimeError('The live write-up was joined without cancellation')
        (Path(data_root()) / 'write-up-settled').write_text('canceled', encoding='utf-8')
    context.controller.walkthrough_executor.submit(work)
    assert entered.wait(2)
    return {'ok': True}
_COMMANDS['_test.live_write_up'] = live_write_up
""")
    folder = tmp_path / "data"
    native = NativeProcess(folder, tmp_path / "stderr.log", source=source)
    try:
        native.ready()
        native.send({"id": 1, "method": "_test.live_write_up"})
        assert native.response(1)["result"]["ok"]
        if request_quit:
            native.send({"id": 2, "method": "app.quit"})
            assert native.response(2)["result"]["ok"]
            assert (folder / "write-up-settled").read_text(encoding="utf-8") == "canceled"
        else:
            native.process.stdin.close()
        native.exited()
        assert (folder / "write-up-settled").read_text(encoding="utf-8") == "canceled"
    finally:
        native.close()


def test_native_rpc_and_app_quit_exit_cleanly_with_parent_pipe_still_open(start_native):
    native = start_native()
    assert native.ready()["data"]["runtime"] == "native"
    native.send({"id": 1, "method": "snapshot"})
    snapshot = native.response(1)["result"]
    assert snapshot["runtime"] == "native" and snapshot["engine"] == "api"
    assert "kp *" in snapshot["hotkey_chords"]
    assert snapshot["hotkeys_suspended"] is False
    native.send({"id": 2, "method": "unknown.command"})
    assert "Unknown native desktop command" in native.response(2)["error"]
    native.send({"id": 3, "method": "snapshot", "params": []})
    assert "Expected a parameter object" in native.response(3)["error"]
    native.send({"id": 4, "method": "app.quit"})
    assert native.response(4)["result"] == {"ok": True}
    assert not native.process.stdin.closed
    native.exited()


def test_native_prompt_allows_rpc_reentry_and_validates_answers(start_native):
    native = start_native()
    native.ready()
    native.send({"id": 1, "method": "_test.prompt"})
    prompt = native.wait_for(lambda p: p.get("event") == "prompt")["data"]
    native.send({"id": 2, "method": "snapshot"})
    assert native.response(2)["result"]["prompts"][0]["id"] == prompt["id"]
    native.send({"id": 3, "method": "prompt.resolve", "params": {"id": prompt["id"], "value": "invalid"}})
    assert "offered responses" in native.response(3)["error"]
    native.send({"id": 4, "method": "prompt.resolve", "params": {"id": prompt["id"], "value": True}})
    assert native.response(4)["result"] == {"ok": True}
    assert native.response(1)["result"] is True
    native.send({"id": 5, "method": "app.quit"})
    native.response(5)
    native.exited()


def test_pipe_eof_cancels_prompt_and_releases_startup_locks(start_native):
    native = start_native()
    native.ready()
    native.send({"id": 1, "method": "_test.prompt"})
    native.wait_for(lambda p: p.get("event") == "prompt")
    native.process.stdin.close()
    native.exited()
    restarted = start_native()
    restarted.ready()
    restarted.send({"id": 2, "method": "app.quit"})
    restarted.response(2)
    restarted.exited()


def test_second_native_process_activates_primary_and_exits(start_native):
    primary = start_native()
    primary.ready()
    secondary = start_native()
    state = secondary.wait_for(lambda p: p.get("event") == "lifecycle")
    assert state["data"]["state"] == "already_running"
    secondary.exited()
    primary.wait_for(lambda p: p.get("event") == "window.show")
    primary.send({"id": 1, "method": "app.quit"})
    primary.response(1)
    primary.exited()
