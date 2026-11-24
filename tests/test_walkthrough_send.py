"""Send a walkthrough to a coding agent: where it can go, what it says, and the button that repeats it."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QToolButton

from services import t3_code
from services.actions.runtime import ActionResult
from services.walkthrough import library, send
from tests.test_walkthrough_library import capture as capture_fixture

capture = capture_fixture
pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "whisper_local"
    folder.mkdir()
    return str(folder)


# ---- the message ----

def test_the_message_asks_for_the_work_then_carries_the_words_and_screenshots(capture):
    result = library.load_result(capture.folder)
    message = result.agent_message()
    assert message.startswith("Carry out the tasks and changes I describe in this walkthrough")
    assert "verify the result" in message
    assert str(result.pdf.resolve()) in message
    assert message.endswith(result.clipboard_text)
    assert str(capture.folder / "shot-1.png") in message
    # Copy for agent asks for the same work, in the same words.
    _, copied = result.clipboard("agent_task")
    assert "verify the result, and report what you changed" in copied


# ---- where it goes ----

def test_destinations_describe_themselves():
    assert send.Destination("t3", "C:/code/whisper_local").place == "a new thread in whisper_local"
    assert send.Destination("t3", "C:/code/x", "t1", "Fix billing").place == "“Fix billing”"
    assert send.Destination("claude_code", "").place == "a new session"
    assert send.Destination("claude_code", "C:/code/x").name == "Claude Code"



def test_folders_chosen_for_agents_are_remembered_newest_first(project, tmp_path):
    assert send.folders() == []
    other = tmp_path / "other"
    other.mkdir()
    send.choose_folder(project)
    send.choose_folder(str(other))
    send.choose_folder(project)
    assert send.folders() == [project, str(other)]


def test_a_folder_that_is_gone_is_left_out(project):
    send.choose_folder(project)
    os.rmdir(project)
    assert send.folders() == []


# ---- sending ----

def _result(text="the walkthrough"):
    return SimpleNamespace(agent_message=lambda: text)


def test_a_new_t3_thread_starts_in_the_project(project):
    with patch.object(t3_code, "start", return_value="t9") as start:
        said = send.send(_result(), send.Destination("t3", project))
    start.assert_called_once_with(project, "the walkthrough")
    assert said == "Started a new thread in whisper_local in T3 Code."


def test_a_chosen_t3_thread_gets_it_as_its_next_message(project):
    with patch.object(t3_code, "send") as sent:
        said = send.send(_result(), send.Destination("t3", project, "t1", "Integrate"))
    sent.assert_called_once_with(project, "t1", "the walkthrough")
    assert said == "Sent to “Integrate” in T3 Code."


def test_t3_code_failures_come_back_as_their_reason(project):
    with patch.object(t3_code, "start", side_effect=t3_code.T3Error("T3 Code isn't running.")):
        with pytest.raises(send.SendError, match="isn't running"):
            send.send(_result(), send.Destination("t3", project))


def test_an_installed_agent_opens_a_session_in_the_project(project):
    with patch.object(send.agents, "handoff", return_value=ActionResult(True, "Opened Codex")) as handoff:
        assert send.send(_result(), send.Destination("codex", project)) == "Sent to Codex."
    handoff.assert_called_once_with("codex", "the walkthrough", folder=project)
    with patch.object(send.agents, "handoff", return_value=ActionResult(False, "Install Codex.")):
        with pytest.raises(send.SendError, match="Install Codex."):
            send.send(_result(), send.Destination("codex", project))



# ---- the menu ----

def _known(*found):
    agent = SimpleNamespace(usable=True)
    return lambda agent_id: (True, agent if agent_id in found else None)


def _texts(menu):
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def _submenu(menu, title):
    return next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == title)


@pytest.fixture
def listing():
    """Set what the menu last heard from T3 Code; forgotten afterwards."""
    from ui_qt.widgets import walkthrough_send

    def heard(threads, error=""):
        walkthrough_send._listed = (walkthrough_send.time.monotonic(), threads, error)

    yield heard
    walkthrough_send._listed = None
    walkthrough_send._listing = False


def test_the_menu_lists_open_threads_from_every_project_then_agents(app, tmp_path, listing):
    from ui_qt.widgets import walkthrough_send

    whisper, t3code = tmp_path / "whisper_local", tmp_path / "t3code"
    whisper.mkdir()
    t3code.mkdir()
    projects = [t3_code.Project("p1", "whisper_local", str(whisper)),
                t3_code.Project("p2", "t3code", str(t3code))]
    listing([t3_code.Thread("t1", "Integrate OpenWhisper with T3 Code", "running", "b", projects[0]),
             t3_code.Thread("t2", "Set Permission Modes", "waiting", "a", projects[1])])
    menu = walkthrough_send.SendMenu(None)
    chosen = Mock()
    menu.chosen.connect(chosen)
    with patch.object(t3_code, "installed", return_value=True), \
            patch.object(t3_code, "projects", return_value=projects), \
            patch.object(walkthrough_send.installed_agents, "known_agent", _known("claude_code")):
        menu.fill()
    try:
        assert _texts(menu) == ["T3 Code", "New thread in",
                                "Integrate OpenWhisper with T3 Code · whisper_local",
                                "Set Permission Modes · t3code", "New session", "Claude Code"]
        next(a for a in menu.actions() if a.text().startswith("Set Permission")).trigger()
        chosen.assert_called_with(send.Destination("t3", str(t3code), "t2", "Set Permission Modes"))

        new_thread = _submenu(menu, "New thread in")
        assert _texts(new_thread) == ["whisper_local", "t3code"]
        new_thread.actions()[1].trigger()
        chosen.assert_called_with(send.Destination("t3", str(t3code)))

        claude = _submenu(menu, "Claude Code")
        assert _texts(claude) == ["whisper_local", "t3code", "Choose a folder…"]
        claude.actions()[0].trigger()
        chosen.assert_called_with(send.Destination("claude_code", str(whisper)))
    finally:
        menu.deleteLater()


def test_a_folder_chosen_for_an_agent_joins_its_places(app, project, listing):
    from ui_qt.widgets import walkthrough_send

    send.choose_folder(project)
    listing([])
    menu = walkthrough_send.SendMenu(None)
    with patch.object(t3_code, "installed", return_value=False), \
            patch.object(walkthrough_send.installed_agents, "known_agent", _known("codex")):
        menu.fill()
    try:
        assert _texts(menu) == ["New session", "Codex"]
        assert _texts(_submenu(menu, "Codex")) == ["whisper_local", "Choose a folder…"]
    finally:
        menu.deleteLater()


@pytest.mark.parametrize("threads, error, note", [
    (None, "T3 Code isn't running. Open it, then send again.",
     "T3 Code isn't running. Open it, then send again."),
    ([], "", "No open threads."),
])
def test_the_menu_says_why_it_shows_no_threads(app, project, listing, threads, error, note):
    from ui_qt.widgets import walkthrough_send

    listing(threads, error)
    menu = walkthrough_send.SendMenu(None)
    with patch.object(t3_code, "installed", return_value=True), \
            patch.object(t3_code, "projects", return_value=[t3_code.Project("p1", "x", project)]), \
            patch.object(walkthrough_send.installed_agents, "known_agent", _known()):
        menu.fill()
    try:
        assert note in _texts(menu)
    finally:
        menu.deleteLater()


def test_with_nowhere_to_send_the_menu_says_what_to_install(app):
    from ui_qt.widgets import walkthrough_send

    menu = walkthrough_send.SendMenu(None)
    with patch.object(t3_code, "installed", return_value=False), \
            patch.object(walkthrough_send.installed_agents, "known_agent", _known()):
        menu.fill()
    try:
        assert _texts(menu) == [walkthrough_send.NOTHING_TO_SEND_TO]
    finally:
        menu.deleteLater()


def test_threads_are_listed_on_a_worker_and_reused_while_fresh(app, listing):
    from ui_qt.widgets import walkthrough_send

    fetched = [t3_code.Thread("t1", "One", "completed", "a", t3_code.Project("p1", "x", "C:/x"))]
    with patch.object(t3_code, "installed", return_value=True), \
            patch.object(t3_code, "recent_threads", return_value=fetched) as recent:
        walkthrough_send.refresh_threads()
        for _ in range(200):
            if walkthrough_send.listed()[0] is not None:
                break
            QTest.qWait(10)
        walkthrough_send.refresh_threads()
    assert walkthrough_send.listed() == (fetched, "", False)
    recent.assert_called_once_with()


# ---- the button ----

def _wait_for(condition):
    for _ in range(300):
        if condition():
            return
        QTest.qWait(10)
    pytest.fail("never happened")


def test_the_button_says_send_to_and_a_click_opens_every_destination(app, project):
    from ui_qt.widgets.walkthrough_send import SendButton, SendMenu

    button = SendButton(name="s", chevron_color="text", chevron_inset=16)
    requested = Mock()
    button.send_requested.connect(requested)
    try:
        assert button.text() == "Send to"
        assert button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        assert isinstance(button.menu(), SendMenu)
        button.send_menu.chosen.emit(send.Destination("claude_code", project))
        requested.assert_called_once_with(send.Destination("claude_code", project))
    finally:
        button.deleteLater()


def test_review_sends_the_version_shown_and_reports_the_answer(app, capture, project):
    from ui_qt.widgets import walkthrough_review
    from ui_qt.widgets.walkthrough_send import relay

    review = walkthrough_review.WalkthroughReview(copy_files=Mock(return_value=True))
    review.show_folder(capture.folder)
    dest = send.Destination("t3", project)
    try:
        with patch.object(walkthrough_review, "start_send", return_value=41) as start:
            review.send_button.send_requested.emit(dest)
        result, sent_to = start.call_args.args
        assert result.folder == capture.folder and sent_to == dest
        assert review.notice.text() == "Sending to T3 Code…"
        relay().sent.emit(40, True, "Someone else's send.")
        assert review.notice.text() == "Sending to T3 Code…"
        relay().sent.emit(41, True, "Started a new thread in whisper_local in T3 Code.")
        assert review.notice.text() == "Started a new thread in whisper_local in T3 Code."
        assert review.send_menu_action.menu() is review.send_button.send_menu
    finally:
        review._dirty = False
        review.close()


def _shown_review(capture, scale):
    from ui_qt.utils.font_scale import apply_ui_font_scale
    from ui_qt.utils.theme_manager import ThemeManager
    from ui_qt.widgets.walkthrough_review import WalkthroughReview

    apply_ui_font_scale(scale, theme_manager=ThemeManager())
    review = WalkthroughReview(closable=True, copy_files=Mock(return_value=True))
    review.show_folder(capture.folder)
    review.title.setText("Saturday, November 21 · 6:24 PM")
    review.show()
    return review


@pytest.mark.parametrize("scale", [100, 115])
def test_review_has_send_to_at_the_walkthrough_tabs_width(app, capture, isolated_app_theme, scale):
    review = _shown_review(capture, scale)
    try:
        review.resize(round(900 * scale / 100), 650)
        QTest.qWait(20)
        assert not review.header_row.stacked
        assert review.send_button.isVisible()
        assert review.send_button.text() == "Send to"
        assert review.copy_button.text() == "Copy PDF"
    finally:
        review.close()


def test_review_gives_send_to_only_room_copy_leaves(app, capture, isolated_app_theme):
    review = _shown_review(capture, 110)
    try:
        title_w = review.title.fontMetrics().horizontalAdvance(review.title.text())
        # Enough room beside the title for Copy on one row, not enough for Send to.
        review.resize(title_w + 280, 650)
        QTest.qWait(20)
        assert not review.header_row.stacked
        assert not review.send_button.isVisible()
        assert review.send_menu_action.isEnabled()
    finally:
        review.close()


def test_the_ready_card_sends_and_says_how_it_went(app, capture, project):
    from ui_qt.widgets import walkthrough_ready
    from ui_qt.widgets.walkthrough_send import relay

    card = walkthrough_ready.WalkthroughReadyCard()
    card.present(capture.result, copied=True)
    dest = send.Destination("t3", project)
    try:
        with patch.object(walkthrough_ready, "start_send", return_value=7) as start:
            card.send_button.send_requested.emit(dest)
            card.send_button.send_requested.emit(dest)
        start.assert_called_once_with(capture.result, dest)
        assert card.subtitle.text() == "Sending to T3 Code…"
        relay().sent.emit(7, False, "T3 Code isn't running. Open it, then send again.")
        assert card.subtitle.text() == "Couldn't send it."
        assert card.reason.isVisibleTo(card)
        assert card.reason.text() == "T3 Code isn't running. Open it, then send again."
    finally:
        card.hide()
        card.deleteLater()


def test_a_send_runs_on_a_worker_and_answers_through_the_relay(app, project):
    from ui_qt.widgets import walkthrough_send

    heard = []

    def hear(*args):
        heard.append(args)

    walkthrough_send.relay().sent.connect(hear)
    walkthrough_send._listed = (walkthrough_send.time.monotonic(), [], "")
    try:
        with patch.object(walkthrough_send.send, "send", return_value="Sent to Codex.") as sending:
            token = walkthrough_send.start_send(_result(), send.Destination("codex", project))
            _wait_for(lambda: heard)
    finally:
        walkthrough_send.relay().sent.disconnect(hear)
    assert heard[-1] == (token, True, "Sent to Codex.")
    assert sending.call_args.args[1] == send.Destination("codex", project)
    # A send changes T3 Code's threads, so the next menu asks again.
    assert walkthrough_send._listed is None
