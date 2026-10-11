"""Program hooks and run_once with real child processes (sys.executable -c …)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from services import settings as settings_module
from services.automation import events, programs
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_agent_process import _alive

PY = sys.executable

#: Appends one JSON line per run: its stdin and the event variables.
RECORD_RUN = (
    "import json,os,sys; data=sys.stdin.buffer.read().decode('utf-8'); "
    "f=open(sys.argv[1],'a',encoding='utf-8'); "
    "f.write(json.dumps({'stdin': data, 'type': os.environ.get('OPENWHISPER_EVENT_TYPE'), "
    "'id': os.environ.get('OPENWHISPER_EVENT_ID'), "
    "'seq': os.environ.get('OPENWHISPER_EVENT_SEQ')})+'\\n'); f.close()"
)


def wait_until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def kill_pid(pid: int) -> None:
    if not _alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal

        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def bus():
    fresh = events.EventBus()
    previous = events.set_bus(fresh)
    yield fresh
    programs.stop()
    fresh.close()
    events.set_bus(previous)


@pytest.fixture
def store(monkeypatch):
    memory = InMemorySettings({})
    monkeypatch.setattr(settings_module, "settings_manager", memory)
    return memory


@pytest.fixture(autouse=True)
def clean_records():
    programs._records.clear()
    yield
    programs.stop()
    programs._records.clear()


def save_programs(store: InMemorySettings, *rows: dict) -> None:
    store.values[SettingsKey.AUTOMATION_PROGRAMS] = [
        {"enabled": True, "events": ["recording.started"], "timeout_s": 30, **row}
        for row in rows
    ]


def program(program_id: str, code: str, *args: str, **extra) -> dict:
    return {"id": program_id, "name": f"Program {program_id}", "program": PY,
            "args": ["-c", code, *args], **extra}


# -- run_once --------------------------------------------------------------------

def test_run_once_passes_stdin_and_returns_stdout_and_exit_code():
    code, out, error = programs.run_once(
        [PY, "-c", "import sys; text = sys.stdin.buffer.read().decode('utf-8'); "
         "sys.stdout.buffer.write(text.upper().encode('utf-8')); sys.exit(3)"],
        "héllo".encode("utf-8"), timeout_s=20,
    )
    assert (code, out.decode("utf-8"), error) == (3, "HÉLLO", "")


def test_run_once_passes_arguments_literally_without_a_shell():
    tricky = 'a & echo pwned | "quoted" $HOME %PATH% ; rm -rf /'
    code, out, error = programs.run_once(
        [PY, "-c", "import sys, json; print(json.dumps(sys.argv[1:]))", tricky, ""],
        b"", timeout_s=20,
    )
    assert (code, error) == (0, "")
    assert json.loads(out) == [tricky, ""]


def test_run_once_starts_its_own_windowless_process_group(monkeypatch):
    from services import agent_process

    seen = {}
    real = agent_process.popen_agent

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real(argv, **kwargs)

    monkeypatch.setattr(agent_process, "popen_agent", spy)
    code, out, error = programs.run_once(
        [PY, "-c", "import os; print(os.name != 'nt' and os.getpgid(0) == os.getpid())"],
        b"", timeout_s=20,
    )
    assert (code, error) == (0, "")
    assert seen["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        # popen_agent adds CREATE_NO_WINDOW and its kill-on-close job.
        assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert out.strip() == b"True"


def test_run_once_keeps_at_most_the_stdout_limit_and_discards_stderr(monkeypatch):
    monkeypatch.setattr(programs, "MAX_STDOUT_BYTES", 1000)
    code, out, error = programs.run_once(
        [PY, "-c", "import sys; sys.stderr.write('E'*300000); sys.stdout.write('x'*300000)"],
        b"", timeout_s=20,
    )
    assert (code, error) == (0, "")
    assert out == b"x" * 1000


def test_run_once_survives_a_program_that_ignores_a_large_stdin():
    started = time.monotonic()
    code, _out, error = programs.run_once([PY, "-c", "pass"], b"x" * (4 * 1024 * 1024),
                                          timeout_s=20)
    assert (code, error) == (0, "")
    assert time.monotonic() - started < 10


def test_run_once_explains_a_missing_program(tmp_path):
    assert programs.run_once([str(tmp_path / "nope.exe")], b"", timeout_s=5) == (
        None, b"", "The program wasn't found.")
    assert programs.run_once([], b"", timeout_s=5)[2] == "Choose the program to run."
    assert programs.run_once([PY], b"", timeout_s=5, cwd=str(tmp_path / "gone"))[2] == (
        "The working folder doesn't exist.")


def test_run_once_runs_in_the_home_folder_unless_told_otherwise(tmp_path):
    show = [PY, "-c", "import os; print(os.getcwd())"]
    _code, out, _error = programs.run_once(show, b"", timeout_s=20)
    assert os.path.samefile(out.decode().strip(), os.path.expanduser("~"))
    _code, out, _error = programs.run_once(show, b"", timeout_s=20, cwd=str(tmp_path))
    assert os.path.samefile(out.decode().strip(), tmp_path)


def test_run_once_strips_openwhispers_secrets_from_the_environment(monkeypatch, tmp_path):
    from tests.test_automation_r3c_child_env import load_dotenv

    load_dotenv(monkeypatch, tmp_path, {"OPENAI_API_KEY": "from-dotenv"})
    monkeypatch.setenv("OPENWHISPER_SIDECAR_TOKEN", "internal")
    monkeypatch.setenv("MY_OWN_SETTING", "kept")
    code, out, _error = programs.run_once(
        [PY, "-c", "import os, json; print(json.dumps({k: os.environ.get(k) for k in "
         "('OPENAI_API_KEY', 'OPENWHISPER_SIDECAR_TOKEN', 'MY_OWN_SETTING')}))"],
        b"", timeout_s=20,
    )
    assert code == 0
    assert json.loads(out) == {"OPENAI_API_KEY": None, "OPENWHISPER_SIDECAR_TOKEN": None,
                               "MY_OWN_SETTING": "kept"}


def test_a_timeout_stops_the_program_and_everything_it_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    started = time.monotonic()
    result = programs.run_once([PY, "-c", code, str(pid_file)], b"", timeout_s=2)
    elapsed = time.monotonic() - started
    child = int(pid_file.read_text())
    try:
        assert result == (None, b"", "Timed out after 2 s")
        assert 1.9 <= elapsed < 6
        wait_until(lambda: not _alive(child), timeout=5)
    finally:
        kill_pid(child)


def test_processes_left_behind_keep_running_when_the_program_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    # The child inherits stdout, so it holds the output open after its parent exits.
    code = (
        "import subprocess, sys; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); print('done', flush=True)"
    )
    started = time.monotonic()
    exit_code, out, error = programs.run_once([PY, "-c", code, str(pid_file)], b"", timeout_s=30)
    child = int(pid_file.read_text())
    try:
        assert (exit_code, out.strip(), error) == (0, b"done", "")
        assert time.monotonic() - started < 10
        time.sleep(1.0)
        assert _alive(child)  # an app a program opens stays open
    finally:
        kill_pid(child)


# -- program hooks -----------------------------------------------------------------

def test_a_program_gets_the_event_on_stdin_and_in_its_environment(tmp_path, bus, store):
    out = tmp_path / "runs.jsonl"
    save_programs(store, program("p", RECORD_RUN, str(out)))
    programs.start()
    assert bus.subscribers("programs") == 1
    event = bus.publish("recording.started", {"job_id": "abc", "mode": "dictation"})

    wait_until(lambda: len(lines(out)) == 1)
    run = lines(out)[0]
    assert run["stdin"] == event.to_json() + "\n"
    assert (run["type"], run["id"], run["seq"]) == ("recording.started", event.id, str(event.seq))
    wait_until(lambda: programs.runs("p"))
    record = programs.runs("p")[0]
    assert (record.event_id, record.ok, record.exit_code, record.error) == (event.id, True, 0, "")


def test_only_enabled_programs_run_and_only_on_their_events(tmp_path, bus, store):
    started, stopped, off = (tmp_path / n for n in ("started", "stopped", "off"))
    save_programs(
        store,
        program("a", RECORD_RUN, str(started)),
        program("b", RECORD_RUN, str(stopped), events=["recording.stopped"]),
        program("c", RECORD_RUN, str(off), enabled=False),
    )
    programs.start()
    programs.start()
    assert bus.subscribers("programs") == 1
    bus.publish("recording.started", {"job_id": "1"})
    bus.publish("recording.stopped", {"job_id": "1", "duration_s": 1})

    wait_until(lambda: len(lines(started)) == 1 and len(lines(stopped)) == 1)
    time.sleep(0.5)
    assert [r["type"] for r in lines(started)] == ["recording.started"]
    assert [r["type"] for r in lines(stopped)] == ["recording.stopped"]
    assert not off.exists()


def test_a_program_runs_one_event_at_a_time_in_order(tmp_path, bus, store):
    log = tmp_path / "log.txt"
    code = (
        "import json, sys, time; n = json.loads(sys.stdin.read())['data']['n']; "
        "f = open(sys.argv[1], 'a'); f.write(f'start {n}\\n'); f.flush(); time.sleep(0.3); "
        "f.write(f'end {n}\\n'); f.close()"
    )
    save_programs(store, program("p", code, str(log)))
    programs.start()
    for n in range(3):
        bus.publish("recording.started", {"n": n})

    wait_until(lambda: log.exists() and log.read_text().count("end") == 3)
    assert log.read_text().split("\n")[:-1] == [
        "start 0", "end 0", "start 1", "end 1", "start 2", "end 2"]


def test_a_full_queue_drops_the_oldest_event(tmp_path, bus, store, monkeypatch):
    monkeypatch.setattr(programs, "MAX_QUEUED", 2)
    log = tmp_path / "log.txt"
    code = (
        "import json, sys, time; n = json.loads(sys.stdin.read())['data']['n']; "
        "time.sleep(0.4); open(sys.argv[1], 'a').write(f'{n}\\n')"
    )
    save_programs(store, program("p", code, str(log)))
    programs.start()
    published = [bus.publish("recording.started", {"n": 0})]
    # The first event is running (taken off the queue) before the rest arrive.
    wait_until(lambda: "p" in programs._runners and not programs._runners["p"].events)
    published += [bus.publish("recording.started", {"n": n}) for n in range(1, 6)]

    wait_until(lambda: sum(1 for r in programs.runs("p") if r.ok) == 3, timeout=20)
    time.sleep(0.6)
    ran = log.read_text().split()
    dropped = [r.event_id for r in programs.runs("p") if r.error.startswith("Dropped")]
    assert len(ran) == 3 and ran[0] == "0" and ran[-2:] == ["4", "5"]
    assert len(dropped) == 3
    assert set(dropped) == {e.id for e in published if str(e.data["n"]) not in ran}


def test_a_hook_that_hangs_is_stopped_at_its_time_limit(tmp_path, bus, store):
    save_programs(store, program("p", "import time; time.sleep(60)", timeout_s=1))
    programs.start()
    bus.publish("recording.started", {"job_id": "1"})
    wait_until(lambda: programs.runs("p"), timeout=10)
    record = programs.runs("p")[0]
    assert (record.ok, record.exit_code, record.error) == (False, None, "Timed out after 1 s")
    assert 900 <= record.elapsed_ms < 6000


def test_a_failing_exit_code_is_recorded(bus, store):
    save_programs(store, program("p", "import sys; sys.exit(2)"))
    programs.start()
    bus.publish("recording.started", {"job_id": "1"})
    wait_until(lambda: programs.runs("p"))
    record = programs.runs("p")[0]
    assert (record.ok, record.exit_code, record.error) == (False, 2, "Exited with code 2")


def test_reload_follows_saved_changes(tmp_path, bus, store):
    out = tmp_path / "runs.jsonl"
    save_programs(store, program("p", RECORD_RUN, str(out)))
    programs.start()
    bus.publish("recording.started", {"job_id": "1"})
    wait_until(lambda: len(lines(out)) == 1)

    save_programs(store, program("p", RECORD_RUN, str(out), events=["recording.canceled"]))
    programs.reload()
    bus.publish("recording.started", {"job_id": "2"})
    bus.publish("recording.canceled", {"job_id": "2"})
    wait_until(lambda: len(lines(out)) == 2)

    save_programs(store, program("p", RECORD_RUN, str(out), enabled=False))
    programs.reload()
    assert bus.subscribers("programs") == 0
    bus.publish("recording.canceled", {"job_id": "3"})
    time.sleep(0.5)
    assert [r["type"] for r in lines(out)] == ["recording.started", "recording.canceled"]


def test_stop_unsubscribes_and_drops_waiting_events(tmp_path, bus, store):
    out = tmp_path / "runs.jsonl"
    save_programs(store, program("p", RECORD_RUN, str(out)))
    programs.start()
    programs.stop()
    programs.stop()
    assert bus.subscribers("programs") == 0
    bus.publish("recording.started", {"job_id": "1"})
    time.sleep(0.4)
    assert not out.exists()


# -- test runs -------------------------------------------------------------------------

def test_run_test_runs_a_program_test_event_even_when_disabled(tmp_path, store):
    out = tmp_path / "runs.jsonl"
    save_programs(store, program("p", RECORD_RUN, str(out), enabled=False))
    started = time.monotonic()
    future = programs.run_test("p")
    assert time.monotonic() - started < 0.5
    record = future.result(timeout=20)

    assert record.ok and record.exit_code == 0
    run = lines(out)[0]
    assert run["type"] == "program.test" and run["id"] == record.event_id
    assert json.loads(run["stdin"])["data"]["program_id"] == "p"
    assert programs.runs("p") == [record]


def test_run_test_for_an_unsaved_program_says_so(store):
    record = programs.run_test("missing").result(timeout=5)
    assert not record.ok and record.error == "Save the program first."


def test_run_test_waits_for_the_running_event_then_goes_first(tmp_path, bus, store):
    log = tmp_path / "log.txt"
    code = (
        "import json, sys, time; e = json.loads(sys.stdin.read()); "
        "f = open(sys.argv[1], 'a'); f.write('start\\n'); f.flush(); time.sleep(0.5); "
        "f.write(e['type'] + '\\n'); f.close()"
    )
    save_programs(store, program("p", code, str(log)))
    programs.start()
    bus.publish("recording.started", {"n": 0})
    bus.publish("recording.started", {"n": 1})
    # The first event is running and the second is waiting.
    wait_until(lambda: log.exists() and "start" in log.read_text()
               and len(programs._runners["p"].events) == 1)
    programs.run_test("p").result(timeout=20)
    wait_until(lambda: len([w for w in log.read_text().split() if w != "start"]) == 3)
    assert [w for w in log.read_text().split() if w != "start"] == [
        "recording.started", "program.test", "recording.started"]


# -- privacy ---------------------------------------------------------------------------

def test_logs_never_carry_arguments_output_or_event_data(tmp_path, bus, store, caplog):
    caplog.set_level(logging.DEBUG)
    code = "import sys; print('program output here'); sys.stderr.write('stderr here'); sys.exit(1)"
    save_programs(store, program("p", code, "secret-argument-123"))
    programs.start()
    bus.publish("recording.started", {"job_id": "dictated words here"})
    wait_until(lambda: programs.runs("p"))
    for needle in ("secret-argument-123", "program output here", "stderr here",
                   "dictated words here", PY):
        assert needle not in caplog.text
