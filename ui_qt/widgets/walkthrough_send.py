"""Send a walkthrough to a coding agent: the ``Send to`` button and its menu.

The menu lists every destination: T3 Code's open threads across all its
projects, a new thread in any of those projects, and a new Claude Code, Codex
or OpenCode session in a project folder. Choosing one sends straight away.

Listing threads and sending run on workers. Workers reach widgets only
through the session's relay (``relay()``), whose signals each widget filters
by the token it was given, never through a widget itself.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QFileDialog, QMenu, QToolButton, QWidget

from services import installed_agents, t3_code
from services.actions.model import AGENT_NAMES, AGENTS
from services.walkthrough import send
from ui_qt.utils.icons import tabler_icon
from ui_qt.utils.palette import token_color
from ui_qt.widgets.context_menu import CONTEXT_MENU_STYLESHEET

logger = logging.getLogger(__name__)

#: How long the thread list is shown before T3 Code is asked again.
FRESH_S = 15.0
#: How many open threads the menu lists.
MAX_THREADS = 8
#: The widest a thread's line is shown, in pixels.
TITLE_WIDTH = 380
#: Each thread's dot, by what it needs.
_STATE_TOKENS = {"working": "accent", "needs you": "warning", "done": "success",
                 "failed": "danger", "stopped": "text-muted", "new": "text-muted"}
NOTHING_TO_SEND_TO = ("Install T3 Code's t3 command, Claude Code, Codex or OpenCode "
                      "to send walkthroughs to them.")


class _Relay(QObject):
    #: The open threads (None when T3 Code couldn't say), and why not.
    listed = pyqtSignal(object, str)
    #: A send's token, whether it went, and what to tell the user.
    sent = pyqtSignal(int, bool, str)


_relay: _Relay | None = None
_tokens = itertools.count(1)
#: (when listed, the open threads or None, why not), or None before a listing.
_listed: tuple[float, list[t3_code.Thread] | None, str] | None = None
_listing = False


def relay() -> _Relay:
    """The session's relay. Made on the Qt thread when first asked for, so it
    lives there even though this module may be imported on a worker."""
    global _relay
    if _relay is None:
        _relay = _Relay()
        # Connected first, so the cache is current before any widget hears.
        _relay.listed.connect(_keep_listed)
        _relay.sent.connect(_forget_listed)
    return _relay


def _keep_listed(threads, error: str) -> None:
    global _listed, _listing
    _listing = False
    _listed = (time.monotonic(), threads, error)


def _forget_listed(_token: int, _ok: bool, _message: str) -> None:
    # A send can start a thread or wake one, so the next menu asks again.
    global _listed
    _listed = None


def listed() -> tuple[list[t3_code.Thread] | None, str, bool]:
    """``(threads, why not, still looking)`` as last heard."""
    _, threads, error = _listed or (0.0, None, "")
    return threads, error, _listing


def refresh_threads() -> None:
    """Ask T3 Code for its open threads, unless that was just done."""
    global _listing
    if _listing or not t3_code.installed():
        return
    if _listed is not None and time.monotonic() - _listed[0] < FRESH_S:
        return
    _listing = True
    signal = relay().listed

    def work():
        try:
            threads, error = t3_code.recent_threads(), ""
        except t3_code.T3Error as exc:
            threads, error = None, str(exc)
        except Exception:
            logger.warning("Listing T3 Code threads failed", exc_info=True)
            threads, error = None, "Couldn't list T3 Code's threads."
        signal.emit(threads, error)

    threading.Thread(target=work, name="walkthrough-send-threads", daemon=True).start()


def start_send(result, dest: send.Destination) -> int:
    """Send ``result`` to ``dest`` on a worker.

    Returns:
        The token ``relay().sent`` reports it with.
    """
    token = next(_tokens)
    signal = relay().sent

    def work():
        try:
            ok, message = True, send.send(result, dest)
        except send.SendError as exc:
            ok, message = False, str(exc)
        except Exception:
            logger.warning("Sending a walkthrough failed", exc_info=True)
            ok, message = False, "Couldn't send the walkthrough."
        signal.emit(token, ok, message)

    threading.Thread(target=work, name="walkthrough-send", daemon=True).start()
    return token


def _dot(token: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(token_color(token))
    painter.drawEllipse(QPointF(8, 8), 4.5, 4.5)
    painter.end()
    return QIcon(pixmap)


class SendMenu(QMenu):
    """Every destination, filled each time it opens."""

    #: The ``send.Destination`` chosen.
    chosen = pyqtSignal(object)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setStyleSheet(CONTEXT_MENU_STYLESHEET)
        self.setToolTipsVisible(True)
        #: This fill's submenus; the next fill deletes them, as clear() does its actions.
        self._submenus: list[QMenu] = []
        self.aboutToShow.connect(self.fill)
        relay().listed.connect(self._on_listed)

    def fill(self) -> None:
        self.clear()
        for menu in self._submenus:
            menu.deleteLater()
        self._submenus = []
        has_t3 = t3_code.installed()
        projects = t3_code.projects() if has_t3 else []
        if has_t3:
            self._t3_section(projects)
        found = [agent_id for agent_id in AGENTS if installed_agents.known_agent(agent_id)[1] is not None]
        if found:
            self.addSeparator()
            self._header("New session")
            places = self._places(projects)
            for agent_id in found:
                self._place_menu(AGENT_NAMES[agent_id], places,
                                 lambda folder, agent_id=agent_id: send.Destination(agent_id, folder),
                                 can_choose=True)
        if not has_t3 and not found:
            self._note(NOTHING_TO_SEND_TO)
        if self.isVisible():
            self.adjustSize()

    def _t3_section(self, projects: list[t3_code.Project]) -> None:
        self._header(send.T3_NAME)
        if projects:
            new = self._place_menu("New thread in", [(p.title, p.folder) for p in projects],
                                   lambda folder: send.Destination(send.T3, folder))
            new.setIcon(tabler_icon("plus-blue.svg"))
        else:
            self._note("No T3 Code projects found on this computer.")
        refresh_threads()
        threads, error, looking = listed()
        if threads:
            metrics = self.fontMetrics()
            for thread in threads[:MAX_THREADS]:
                line = metrics.elidedText(f"{thread.title} · {thread.project.title}",
                                          Qt.TextElideMode.ElideMiddle, TITLE_WIDTH)
                action = self.addAction(_dot(_STATE_TOKENS[thread.state]), line)
                action.setToolTip(f"{thread.state.capitalize()} in {thread.project.title}: "
                                  "send this walkthrough as its next message")
                action.triggered.connect(lambda _=False, thread=thread: self.chosen.emit(
                    send.Destination(send.T3, thread.project.folder, thread.id, thread.title)))
        elif looking:
            self._note("Looking for open threads…")
        elif error:
            self._note(error)
        elif threads is not None:
            self._note("No open threads.")

    @staticmethod
    def _places(projects: list[t3_code.Project]) -> list[tuple[str, str]]:
        """``(name, folder)`` for each project folder: T3 Code's, then ones chosen here."""
        places = [(project.title, project.folder) for project in projects]
        seen = {os.path.normcase(os.path.normpath(folder)) for _, folder in places}
        for folder in send.folders():
            if os.path.normcase(os.path.normpath(folder)) not in seen:
                places.append((send.folder_name(folder), folder))
        return places

    def _place_menu(self, title: str, places, make, *, can_choose: bool = False) -> QMenu:
        """A submenu of project folders; choosing one sends to ``make(folder)``."""
        # No sheet of its own: it inherits this menu's, already resolved. One set
        # here, while this menu opens, would keep its @tokens and Qt would drop it.
        menu = QMenu(title, self)
        menu.setToolTipsVisible(True)
        for name, folder in places:
            action = menu.addAction(name)
            action.setToolTip(folder)
            action.triggered.connect(lambda _=False, folder=folder: self.chosen.emit(make(folder)))
        if can_choose:
            if places:
                menu.addSeparator()
            menu.addAction("Choose a folder…").triggered.connect(lambda: self._ask_folder(make))
        self._submenus.append(menu)
        self.addMenu(menu)
        return menu

    def _header(self, text: str) -> None:
        action = self.addAction(text)
        font = QFont(action.font())
        font.setBold(True)
        action.setFont(font)
        action.setEnabled(False)

    def _note(self, text: str) -> None:
        self.addAction(text).setEnabled(False)

    def _on_listed(self, _threads, _error: str) -> None:
        if self.isVisible():
            self.fill()

    def _ask_folder(self, make) -> None:
        # No parent: the ready card that may own this menu never takes focus.
        folder = QFileDialog.getExistingDirectory(None, "Choose the project folder",
                                                  os.path.expanduser("~"))
        if folder:
            folder = os.path.normpath(folder)
            send.choose_folder(folder)
            self.chosen.emit(make(folder))


class SendButton(QToolButton):
    """``Send to ⌄``: a click opens every destination, and choosing one sends.

    Hosts style it by object name, leaving room on its right for the chevron
    painted ``chevron_inset`` pixels from its edge, and send on ``send_requested``.
    """

    #: The ``send.Destination`` to send the walkthrough to.
    send_requested = pyqtSignal(object)

    def __init__(self, *, name: str, chevron_color: str, chevron_inset: int,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setText("Send to")
        self.setToolTip("Send this walkthrough to a coding agent")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._chevron_color = chevron_color
        self._chevron_inset = chevron_inset
        self.send_menu = SendMenu(self)
        self.send_menu.chosen.connect(self.send_requested)
        self.setMenu(self.send_menu)

    @staticmethod
    def look_ahead() -> None:
        """Ask T3 Code for its open threads before the menu needs them."""
        refresh_threads()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.5)
        painter.setPen(QPen(token_color(self._chevron_color), 1.5, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx, cy = self.width() - self._chevron_inset, self.height() / 2.0
        painter.drawLine(QPointF(cx - 3.2, cy - 1.5), QPointF(cx, cy + 1.8))
        painter.drawLine(QPointF(cx, cy + 1.8), QPointF(cx + 3.2, cy - 1.5))
        painter.end()
