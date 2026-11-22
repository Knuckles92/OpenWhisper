"""T3 Code's t3 command: found where T3 Code puts it, words only on stdin, threads and errors read right."""
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from services import t3_code


class _Proc:
    """A finished ``t3`` run that records what it was given."""

    def __init__(self, calls, *, out="", err="", code=0, hang=False):
        self._calls, self._out, self._err, self._code, self._hang = calls, out, err, code, hang
        self.returncode = None

    def communicate(self, data, timeout):
        self._calls[-1]["input"] = data.decode("utf-8")
        self._calls[-1]["timeout"] = timeout
        if self._hang:
            raise subprocess.TimeoutExpired("t3", timeout)
        self.returncode = self._code
        return self._out.encode("utf-8"), self._err.encode("utf-8")


@pytest.fixture
def t3(tmp_path, monkeypatch):
    """A ``t3`` on PATH whose runs answer ``t3.answer`` and are recorded in ``t3.calls``."""
    calls = []
    answer = {"out": "", "err": "", "code": 0, "hang": False}

    def popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return _Proc(calls, out=answer["out"], err=answer["err"], code=answer["code"],
                     hang=answer["hang"])

    monkeypatch.setattr(t3_code.shutil, "which", lambda name: "C:/t3/t3.cmd" if name == "t3" else None)
    monkeypatch.setattr(t3_code, "popen_agent", popen)
    monkeypatch.setattr(t3_code, "kill_agent_process", lambda proc: None)
    project = tmp_path / "my & 100% project"
    project.mkdir()

    class T3:
        pass

    T3.calls, T3.answer, T3.project = calls, answer, str(project)
    return T3


def _listing(*items):
    return json.dumps(list(items))


def _project(folder, project_id="p1", title="whisper_local"):
    return t3_code.Project(project_id, title, folder)


def _item(thread_id, title, status="completed", settled=False, updated="2026-11-21T10:00:00.000Z"):
    return {"threadId": thread_id, "title": title, "status": status, "settled": settled,
            "updatedAt": updated, "projectId": "p1"}


# ---- finding the command ----

def test_the_command_on_path_wins(monkeypatch):
    monkeypatch.setattr(t3_code.shutil, "which", lambda name: "/usr/local/bin/t3")
    assert t3_code.command() == ["/usr/local/bin/t3"]


@pytest.mark.parametrize("platform, relative", [
    ("win32", Path(".t3/bin/t3.cmd")),
    ("linux", Path(".local/bin/t3")),
    ("darwin", Path(".t3/bin/t3")),
])
def test_without_path_it_is_found_where_t3_code_installs_it(tmp_path, monkeypatch, platform, relative):
    monkeypatch.setattr(t3_code.shutil, "which", lambda name: None)
    monkeypatch.setattr(t3_code.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(t3_code.sys, "platform", platform)
    assert t3_code.command() is None
    shim = tmp_path / relative
    shim.parent.mkdir(parents=True)
    shim.write_text("")
    assert t3_code.command() == [str(shim)]
    assert t3_code.installed()


def test_without_the_command_every_call_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(t3_code, "command", lambda: None)
    with pytest.raises(t3_code.T3Error, match="Settings → About"):
        t3_code._threads(_project(str(tmp_path)))


# ---- what reaches the command ----

def test_words_and_the_folder_never_reach_the_command_line(t3):
    words = 'fix "this" & that | 100% done'
    t3.answer["out"] = json.dumps({"threadId": "thread-1", "projectId": "p1", "runId": "r1"})
    assert t3_code.start(t3.project, words) == "thread-1"
    call = t3.calls[-1]
    assert call["argv"] == ["C:/t3/t3.cmd", "thread", "start", ".", "-", "--generate-title", "--json"]
    assert call["cwd"] == t3.project
    assert call["stdin"] == subprocess.PIPE
    assert call["input"] == words
    assert call["timeout"] == t3_code.TIMEOUT_S


def test_a_message_goes_to_a_thread_by_its_id(t3):
    t3_code.send(t3.project, "7b9c2a73-c51a-4190", "and also the board")
    assert t3.calls[-1]["argv"][1:] == ["thread", "send", "7b9c2a73-c51a-4190", "-", "--json"]
    assert t3.calls[-1]["input"] == "and also the board"


@pytest.mark.parametrize("thread_id", ["--project", "", "a b", "x;y"])
def test_an_id_that_could_read_as_anything_else_is_refused(t3, thread_id):
    with pytest.raises(t3_code.T3Error):
        t3_code.send(t3.project, thread_id, "words")
    assert t3.calls == []


def test_a_started_thread_whose_answer_cant_be_read_still_counts(t3):
    t3.answer["out"] = "not json"
    assert t3_code.start(t3.project, "words") == ""


# ---- reading threads ----

def test_a_projects_threads_are_its_unsettled_ones(t3):
    t3.answer["out"] = _listing(
        _item("old", "Old work", updated="2026-11-20T10:00:00.000Z"),
        _item("gone", "Finished", settled=True, updated="2026-11-22T10:00:00.000Z"),
        _item("new", "Integrate with T3 Code", status="running", updated="2026-11-21T10:00:00.000Z"),
        _item("--bad", "A flag", updated="2026-11-23T10:00:00.000Z"),
        {"threadId": 5, "title": "Wrong types"},
    )
    found = t3_code._threads(_project(t3.project))
    assert [thread.id for thread in found] == ["old", "new"]
    assert found[1].title == "Integrate with T3 Code" and found[1].state == "working"
    assert found[1].project.title == "whisper_local"
    assert t3.calls[-1]["argv"][1:] == ["thread", "list", "--project", "p1", "--json"]


def _database(home, rows):
    """T3 Code's database with just its projects table, as T3 Code lays it out."""
    path = home / "userdata" / "statev2.sqlite"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE projection_projects (project_id TEXT, title TEXT, workspace_root TEXT, "
                   "deleted_at TEXT, archived_at TEXT)")
        db.executemany("INSERT INTO projection_projects VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()
    return path


def test_projects_come_from_t3_codes_own_list(tmp_path, monkeypatch):
    home = tmp_path / ".t3 home"
    monkeypatch.setenv("T3CODE_HOME", str(home))
    assert t3_code.projects() == []
    live, gone = tmp_path / "whisper_local", tmp_path / "deleted"
    live.mkdir()
    _database(home, [
        ("p1", "whisper_local", str(live), None, None),
        ("p2", "Deleted", str(live), "2026-11-01", None),
        ("p3", "Archived", str(live), None, "2026-11-01"),
        ("p4", "Folder gone", str(gone), None, None),
        ("--flag", "Bad id", str(live), None, None),
    ])
    assert t3_code.projects() == [t3_code.Project("p1", "whisper_local", str(live))]


def test_a_database_laid_out_differently_gives_no_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("T3CODE_HOME", str(tmp_path))
    path = tmp_path / "userdata" / "statev2.sqlite"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE something_else (x TEXT)")
    assert t3_code.projects() == []


def test_recent_threads_merge_every_project_newest_first(t3, tmp_path, monkeypatch):
    other = tmp_path / "t3code"
    other.mkdir()
    projects = [_project(t3.project), _project(str(other), "p2", "t3code")]
    monkeypatch.setattr(t3_code, "projects", lambda: projects)
    answers = {"p1": _listing(_item("a", "Older", updated="2026-11-20T10:00:00.000Z")),
               "p2": _listing(_item("b", "Newer", updated="2026-11-21T10:00:00.000Z"))}

    def listed(project):
        return [t3_code.Thread(item["threadId"], item["title"], item["status"], item["updatedAt"], project)
                for item in json.loads(answers[project.id])]

    monkeypatch.setattr(t3_code, "_threads", listed)
    found = t3_code.recent_threads()
    assert [(thread.title, thread.project.title) for thread in found] == [
        ("Newer", "t3code"), ("Older", "whisper_local")]


def test_when_no_project_can_be_listed_the_reason_comes_back(t3, monkeypatch):
    monkeypatch.setattr(t3_code, "projects", lambda: [_project(t3.project)])
    t3.answer.update(err="No running T3 Code server uses C:\\x. Start T3 Code, then retry.", code=1)
    with pytest.raises(t3_code.T3Error, match="isn't running"):
        t3_code.recent_threads()


@pytest.mark.parametrize("status, state", [
    ("running", "working"), ("queued", "working"), ("waiting", "needs you"),
    ("completed", "done"), ("failed", "failed"), ("interrupted", "stopped"),
    ("cancelled", "stopped"), ("idle", "new"),
])
def test_each_status_reads_as_what_it_needs(status, state):
    assert t3_code.Thread("t", "x", status, "", _project("C:/p")).state == state


def test_a_listing_that_isnt_json_says_so(t3):
    t3.answer["out"] = "<html>"
    with pytest.raises(t3_code.T3Error, match="can't read"):
        t3_code._threads(_project(t3.project))


# ---- failures ----

@pytest.mark.parametrize("stderr, said", [
    ("No running T3 Code server uses C:\\Users\\me\\.t3. Start T3 Code, then retry.",
     "T3 Code isn't running. Open it, then send again."),
    ("No T3 Code project matches '.'. Pass a project id or a path inside a project.",
     "That folder isn't a project in T3 Code. Add it there, or choose another folder."),
    ("warming up\nThread t1 is waiting for an answer. Open it in T3 Code to respond.",
     "Thread t1 is waiting for an answer. Open it in T3 Code to respond."),
    ("", "T3 Code didn't take it."),
])
def test_failures_say_why_in_words_for_this_app(t3, stderr, said):
    t3.answer.update(err=stderr, code=1)
    with pytest.raises(t3_code.T3Error) as raised:
        t3_code.send(t3.project, "t1", "words")
    assert str(raised.value) == said


def test_a_hung_command_times_out_with_a_reason(t3):
    t3.answer["hang"] = True
    with pytest.raises(t3_code.T3Error, match="didn't answer in time"):
        t3_code._threads(_project(t3.project))


def test_a_project_folder_that_is_gone_is_refused_before_running(t3, tmp_path):
    with pytest.raises(t3_code.T3Error, match="doesn't exist"):
        t3_code._threads(_project(str(tmp_path / "deleted")))
    assert t3.calls == []
