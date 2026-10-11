"""The action editor: what a new or saved action does, how its words are prepared, how it starts."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
)

from services.actions.model import (
    DESCRIPTION_MAX_CHARS,
    NAME_MAX_CHARS,
    PARAMS,
    TEMPLATE_MAX_CHARS,
    Action,
    ActionKind,
    action_hotkey_conflict,
    expand_link,
    find_action,
    save_action,
    validate_action,
)
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import action_relay
from ui_qt.widgets.action_copy import (
    KIND_ICONS,
    KIND_BLURBS,
    SAMPLE_WORDS,
    parse_arguments,
    parse_spoken_names,
)
from ui_qt.widgets.action_fields import (
    AgentFields,
    Columns,
    button_row,
    OutputCombo,
    PathField,
    ShapingField,
    ShortcutField,
    field_label,
    hint_label,
    input_field,
    page_link,
    plain_row,
    section_heading,
    stack,
    tint_icon,
)
from ui_qt.widgets.buttons import Button, compact_primary_button, fit_compact_button, neutral_button
from ui_qt.widgets.no_wheel import NoWheelSpinBox
from ui_qt.widgets.reflow import Reflow
from ui_qt.widgets.setting_tile import InfoTile
from ui_qt.widgets.wrapped_label import WrappedLabel

_KIND_NOTES = {
    ActionKind.CHANNEL: (
        "Needs a Claude Code session running the OpenWhisper channel. With none "
        "listening, your words are copied instead."
    ),
    ActionKind.CLIPBOARD: "Your words are copied, ready to paste anywhere. Nothing is pasted for you.",
    ActionKind.SCRIPTS: (
        "Nothing happens here: programs following OpenWhisper's live events "
        "see your words and act on them."
    ),
}


def copy_text(text: str) -> None:
    """Put ``text`` on the clipboard (patched by tests)."""
    QApplication.clipboard().setText(text)


class ActionEditor(InfoTile):
    """Edits one action; nothing is saved until Save (or leaving Settings with a valid draft)."""

    saved = pyqtSignal(object)
    cancelled = pyqtSignal()
    capture_changed = pyqtSignal(bool)
    #: A link to another Settings page: "styles" or "mcp".
    destination_requested = pyqtSignal(str)

    def __init__(self, *, manager, try_action: Callable, parent=None):
        super().__init__("New action", "", design_icon("bolt-green.svg"), parent)
        self.setProperty("tileId", "actionEditor")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_body_indented(False)
        self.manager = manager
        self._try_action = try_action
        self._baseline: Optional[Action] = None
        self._action: Optional[Action] = None
        self._is_new = False
        self._try_token = 0
        self._scan_token = 0
        self._agents_scanned = False
        self._secret_token = 0
        self._secret_renew = False
        self._secret_made = False

        self.name_edit = input_field(QLineEdit())
        self.name_edit.setMaxLength(NAME_MAX_CHARS)
        self.name_edit.setPlaceholderText("e.g. Ask Claude")
        self.add_body(stack(field_label("Name", self.name_edit), self.name_edit))

        # -- open a link --
        self.template_edit = input_field(QLineEdit())
        self.template_edit.setMaxLength(TEMPLATE_MAX_CHARS)
        self.template_edit.setPlaceholderText("https://www.perplexity.ai/search?q={text}")
        self.template_edit.setAccessibleName("Link")
        self.template_edit.textChanged.connect(self._update_preview)
        self.template_hint = hint_label("{text} becomes your words. Links only fill things in; you send.")
        self.preview = WrappedLabel("", break_long_words=True)
        self.preview.setObjectName("actionLinkPreview")
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preset_note = hint_label("")
        self.preset_note.setProperty("tone", "warning")
        self.link_box = stack(field_label("Link", self.template_edit), self.template_edit,
                                 self.template_hint, self.preview, self.preset_note)
        self.add_body(self.link_box)

        # -- an agent --
        self.agent_fields = AgentFields()
        self.agent_fields.styles_requested.connect(lambda: self.destination_requested.emit("styles"))
        self.agent_box = stack(self.agent_fields)
        self.add_body(self.agent_box)

        # -- a webhook --
        self.url_edit = input_field(QLineEdit())
        self.url_edit.setPlaceholderText("https://example.com/hooks/openwhisper")
        self.url_edit.setAccessibleName("Address")
        self.url_edit.textChanged.connect(self._update_url_note)
        self.url_note = hint_label("")
        self.url_note.setProperty("tone", "warning")
        self.reply_combo = OutputCombo("Where the reply goes")
        self.copy_secret_button = neutral_button(Button("Copy secret"))
        self.copy_secret_button.setAccessibleName("Copy signing secret")
        self.copy_secret_button.clicked.connect(self.copy_secret)
        self.new_secret_button = neutral_button(Button("New secret"))
        self.new_secret_button.clicked.connect(self.new_secret)
        self.secret_hint = hint_label("Your service checks each request with it.")
        self.secret_hint.setToolTip(
            "Requests are signed the Standard Webhooks way: webhook-id, "
            "webhook-timestamp and webhook-signature headers.")
        self.webhook_box = stack(
            field_label("Address", self.url_edit), self.url_edit, self.url_note,
            Columns(
                stack(field_label("Reply goes to", self.reply_combo), self.reply_combo),
                stack(field_label("Signing secret"),
                      button_row(self.copy_secret_button, self.new_secret_button),
                      self.secret_hint),
                min_column=240,
            ),
        )
        self.add_body(self.webhook_box)

        # -- a program --
        self.program_field = PathField("Program", folder=False, placeholder="Choose a program or script")
        self.args_edit = input_field(QPlainTextEdit())
        self.args_edit.setObjectName("actionArgsEdit")
        self.args_edit.setAccessibleName("Arguments, one per line")
        self.args_edit.setPlaceholderText("--flag\nvalue")
        self.args_edit.setTabChangesFocus(True)
        self.args_edit.setFixedHeight(68)
        self.program_output = OutputCombo("Where the output goes")
        self.timeout_spin = input_field(NoWheelSpinBox())
        self.timeout_spin.setRange(1, 300)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setAccessibleName("Time limit in seconds")
        self.timeout_spin.setMinimumHeight(36)
        self.timeout_spin.setMaximumWidth(160)
        output_column = stack(field_label("Output goes to", self.program_output), self.program_output)
        timeout_column = stack(field_label("Time limit", self.timeout_spin), self.timeout_spin)
        output_row = Columns(output_column, timeout_column, min_column=200)
        self.program_box = stack(
            field_label("Program"), self.program_field,
            hint_label("Your words arrive on its input. It never runs through a shell."),
            field_label("Arguments (one per line)", self.args_edit), self.args_edit,
            output_row,
        )
        self.add_body(self.program_box)

        # -- channel, clipboard, scripts --
        self.kind_note = hint_label("")
        self.mcp_link = page_link("Set up the channel", "Settings → Automate → API & MCP")
        self.mcp_link.clicked.connect(lambda: self.destination_requested.emit("mcp"))
        self.note_box = stack(self.kind_note, plain_row(self.mcp_link, stretch_last=True))
        self.add_body(self.note_box)

        # -- for every kind --
        self.shaping = ShapingField()
        self.add_body(section_heading("Your words"))
        self.add_body(self.shaping)

        self.shortcut = ShortcutField("Action shortcut")
        self.shortcut.captured.connect(self._on_shortcut_captured)
        self.shortcut.capture_changed.connect(self.capture_changed)
        self.spoken_edit = input_field(QLineEdit())
        self.spoken_edit.setPlaceholderText("e.g. Claude, ask Claude")
        self.spoken_edit.setAccessibleName("Spoken names, separated by commas")
        self.description_edit = input_field(QLineEdit())
        self.description_edit.setMaxLength(DESCRIPTION_MAX_CHARS)
        self.description_edit.setPlaceholderText("What it's for, e.g. Questions for Claude")
        self.description_edit.setAccessibleName("What it's for")
        self.add_body(section_heading("How you start it"))
        self.start_columns = Columns(
            stack(field_label("Shortcut (optional)", self.shortcut.input), self.shortcut,
                  hint_label("Press it once to start, speak, then press it again.")),
            stack(field_label("Spoken names", self.spoken_edit), self.spoken_edit,
                  hint_label("With Route by voice, say one first: “Claude, …”")),
        )
        self.add_body(self.start_columns)
        purpose = stack(
            field_label("What it's for", self.description_edit), self.description_edit,
            hint_label("Route by voice picks the right action more reliably when it says "
                       "what it's for."),
        )
        purpose.layout().setContentsMargins(0, 6, 0, 0)
        self.add_body(purpose)

        self.sample_edit = input_field(QLineEdit(SAMPLE_WORDS))
        self.sample_edit.setAccessibleName("Words to try it with")
        self.sample_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.try_button = neutral_button(Button("Try it"))
        fit_compact_button(self.try_button)
        self.try_button.clicked.connect(self.try_it)
        try_row = Reflow(self.sample_edit, self.try_button, lead_width=180,
                         stacked_alignment=Qt.AlignmentFlag.AlignLeft)
        self.try_result = hint_label("")
        self.try_result.setObjectName("actionTryResult")
        self.try_result.hide()
        self.add_body(section_heading("Try it"))
        self.add_body(stack(try_row, self.try_result))

        self.message = hint_label("")
        self.message.setProperty("tone", "error")
        self.message.hide()
        self.add_body(self.message)
        self.cancel_button = neutral_button(Button("Cancel"))
        self.cancel_button.clicked.connect(self.cancel)
        self.save_button = compact_primary_button(Button("Save action"))
        self.save_button.clicked.connect(self.save)
        self.add_body(button_row(self.cancel_button, self.save_button, align="right"))
        relay = action_relay.relay()
        relay.try_done.connect(self._on_try_done)
        relay.scan_done.connect(self._on_scan_done)
        relay.secret_done.connect(self._on_secret_done)

    # ---- opening ----

    @property
    def action_id(self) -> str:
        return self._action.id if self._action is not None else ""

    @property
    def is_new(self) -> bool:
        return self._is_new

    def open(self, action: Action, *, is_new: bool, note: str = "") -> None:
        """Show ``action``; a new one is only saved once you save it."""
        self.shortcut.cancel_capture()
        self._action = action
        self._is_new = is_new
        self._secret_made = False
        self._secret_token = 0
        self._set_secret_busy(False)
        settings = self._settings()
        self.title_label.setText("New action" if is_new else f"Edit {action.name}")
        self.set_description(KIND_BLURBS.get(action.kind, ""))
        tint_icon(self, KIND_ICONS[action.kind], indent_body=False)
        self.name_edit.setText(action.name)
        kind = action.kind
        # Every kind's fields are filled, with the kind's defaults where they
        # don't apply, so no box keeps the last action's values.
        link, hook, program = (
            action if kind == other else Action(action.id, "", other)
            for other in (ActionKind.OPEN_LINK, ActionKind.WEBHOOK, ActionKind.PROGRAM)
        )
        self.template_edit.setText(link.param("template") or "")
        self.preset_note.setText(note)
        self.preset_note.setVisible(bool(note))
        self.agent_fields.set_params(action.params if kind == ActionKind.AGENT else {})
        from services.settings import resolve_app_context_enabled
        from ui_qt.widgets.action_agent_card import available_agents

        self.agent_fields.set_app_awareness(resolve_app_context_enabled(dict(settings)))
        if kind == ActionKind.AGENT:
            self._show_agents(available_agents())
        self.url_edit.setText(hook.param("url") or "")
        self.reply_combo.set_value(hook.param("output"))
        self.program_field.set_path(program.param("program") or "")
        self.args_edit.setPlainText("\n".join(program.param("args") or []))
        self.program_output.set_value(program.param("output"))
        self.timeout_spin.setValue(int(program.param("timeout_s") or 30))
        self.kind_note.setText(_KIND_NOTES.get(kind, ""))
        self.mcp_link.setVisible(action.kind == ActionKind.CHANNEL)
        self.shaping.set_choices(settings)
        self.shaping.set_value(action.shaping)
        self.spoken_edit.setText(", ".join(action.spoken_names))
        self.description_edit.setText(action.description)
        for edit in (self.name_edit, self.template_edit, self.url_edit, self.spoken_edit,
                     self.description_edit, self.sample_edit):
            edit.setCursorPosition(0)
        self.shortcut.set_hotkey(action.hotkey)
        self.try_result.hide()
        self.try_button.setEnabled(True)
        self._try_token = 0
        self.show_message("")
        for kind, box in (
            (ActionKind.OPEN_LINK, self.link_box),
            (ActionKind.AGENT, self.agent_box),
            (ActionKind.WEBHOOK, self.webhook_box),
            (ActionKind.PROGRAM, self.program_box),
        ):
            box.setVisible(action.kind == kind)
        self.note_box.setVisible(action.kind in _KIND_NOTES)
        self._update_preview()
        self._update_url_note()
        # The draft as opened: untouched, there is nothing to keep.
        self._baseline = self.draft()

    def _show_agents(self, found: dict) -> None:
        """What the last scan found, or look once when nothing has been scanned yet."""
        if found or self._agents_scanned:
            self.agent_fields.set_available(found)
            return
        self._agents_scanned = True
        self._scan_token = token = action_relay.next_token()
        self.agent_fields.set_available(None)
        action_relay.start_agent_scan(token)

    def _on_scan_done(self, token: int) -> None:
        if token != self._scan_token:
            return
        from ui_qt.widgets.action_agent_card import available_agents

        self.agent_fields.set_available(available_agents(), animate=True)

    def refresh_choices(self, settings: Mapping) -> None:
        """Profiles, transforms and app awareness may have changed on other pages."""
        from services.settings import resolve_app_context_enabled

        self.shaping.set_choices(settings)
        self.agent_fields.set_app_awareness(resolve_app_context_enabled(dict(settings)))

    def _settings(self) -> dict:
        return self.manager.load_all_settings()

    # ---- the draft ----

    def draft(self) -> Action:
        action = self._action
        kind = action.kind
        if kind == ActionKind.OPEN_LINK:
            params = {"template": self.template_edit.text().strip()}
        elif kind == ActionKind.AGENT:
            params = self.agent_fields.params()
        elif kind == ActionKind.WEBHOOK:
            params = {"url": self.url_edit.text().strip(), "output": self.reply_combo.value()}
        elif kind == ActionKind.PROGRAM:
            params = {
                "program": self.program_field.path(),
                "args": parse_arguments(self.args_edit.toPlainText()),
                "output": self.program_output.value(),
                "timeout_s": self.timeout_spin.value(),
            }
        else:
            params = {}
        params = {key: params[key] for key in PARAMS.get(kind, {}) if key in params}
        return replace(
            action,
            name=self.name_edit.text().strip(),
            params=params,
            hotkey=self.shortcut.hotkey,
            shaping=self.shaping.value(),
            spoken_names=parse_spoken_names(self.spoken_edit.text()),
            description=self.description_edit.text().strip(),
        )

    def has_unsaved_changes(self) -> bool:
        return self._action is not None and self._baseline is not None and self.draft() != self._baseline

    # ---- live hints ----

    def _update_preview(self) -> None:
        template = self.template_edit.text().strip()
        if not template:
            self.preview.setText("")
            self.preview.hide()
            return
        try:
            link = expand_link(template, self.sample_edit.text() or SAMPLE_WORDS)
        except ValueError as exc:
            text, tone = str(exc), "warning"
        else:
            text = f"Opens {link}"
            tone = ""
            if "{text}" not in template:
                text, tone = "Add {text} where your words should go.", "warning"
        self.preview.setText(text)
        set_style_property(self.preview, "tone", tone)
        self.preview.show()

    def _update_url_note(self) -> None:
        from services.automation.models import url_is_private

        url = self.url_edit.text().strip()
        unencrypted = url.lower().startswith("http://") and not url_is_private(url)
        self.url_note.setText("Your words travel unencrypted to this address." if unencrypted else "")
        self.url_note.setVisible(unencrypted)

    def show_message(self, text: str, tone: str = "error") -> None:
        self.message.setText(text)
        set_style_property(self.message, "tone", tone)
        self.message.setVisible(bool(text))

    # ---- shortcut ----

    def _on_shortcut_captured(self, hotkey: str) -> None:
        conflict = action_hotkey_conflict(hotkey, self._settings(), exclude_id=self.action_id)
        if conflict:
            self.shortcut.restore()
            self.show_message(f"That shortcut is already used by {conflict}. Choose another.")
            return
        self.shortcut.set_hotkey(hotkey)
        self.show_message("")

    def cancel_capture(self) -> None:
        self.shortcut.cancel_capture()

    # ---- signing secret ----

    def copy_secret(self) -> None:
        """Copy this action's signing secret, making one the first time."""
        self._fetch_secret(renew=False)

    def new_secret(self) -> None:
        """Replace the signing secret and copy the new one."""
        if not self._is_new:
            # The service already checks requests with the old one.
            answer = QMessageBox.question(
                self, "Make a new signing secret?",
                "Your service will need the new secret before it accepts requests again.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._fetch_secret(renew=True)

    def _fetch_secret(self, *, renew: bool) -> None:
        self._secret_token = token = action_relay.next_token()
        self._secret_renew = renew
        if self._is_new:
            self._secret_made = True
        self._set_secret_busy(True)
        self.show_message("Making a new signing secret…" if renew else "Getting the signing secret…", "")
        action_relay.fetch_secret(token, self.action_id, renew=renew)

    def _set_secret_busy(self, busy: bool) -> None:
        self.copy_secret_button.setEnabled(not busy)
        self.new_secret_button.setEnabled(not busy)

    def _on_secret_done(self, token: int, secret: str, error: str) -> None:
        if token != self._secret_token or not token:
            return
        self._secret_token = 0
        self._set_secret_busy(False)
        if not secret:
            self.show_message(f"Couldn't get the signing secret: {error or 'nothing was saved'}")
            return
        copy_text(secret)
        self.show_message(
            "New signing secret copied. Update your service with it." if self._secret_renew
            else "Signing secret copied. Paste it into your service.", "ok")

    def _drop_unsaved_secret(self) -> None:
        """A new action that's never saved leaves no secret behind."""
        if self._is_new and self._secret_made and self._action is not None:
            action_relay.forget_secret(self._action.id)
        self._secret_made = False

    # ---- trying ----

    def try_it(self) -> None:
        if self._action is None:
            return
        draft = self.draft()
        probe = replace(draft, name=draft.name or "Untitled action", hotkey="", spoken_names=())
        try:
            validate_action(probe, self._settings())
        except ValueError as exc:
            self._show_try_result(str(exc), "error")
            return
        self._try_token = token = action_relay.next_token()
        self.try_button.setEnabled(False)
        self._show_try_result("Trying…", "")
        self._try_action(probe, self.sample_edit.text().strip() or SAMPLE_WORDS,
                         action_relay.try_callback(token))

    def _on_try_done(self, token: int, result) -> None:
        if token != self._try_token or self._try_token == 0:
            return
        self._try_token = 0
        self.try_button.setEnabled(True)
        ok = bool(getattr(result, "ok", False))
        message = getattr(result, "message", "") or ("It worked." if ok else "It didn't work.")
        output = getattr(result, "output", None)
        if output:
            message = f"{message}\n{output}"
        self._show_try_result(message, "ok" if ok else "error")

    def _show_try_result(self, text: str, tone: str) -> None:
        self.try_result.setText(text)
        set_style_property(self.try_result, "tone", tone)
        self.try_result.setVisible(bool(text))

    # ---- saving ----

    def save(self) -> bool:
        """Save the draft; the reason it can't be saved shows above the buttons."""
        if self._action is None:
            return True
        self.shortcut.cancel_capture()
        action = self.draft()
        try:
            self.manager.mutate_settings(lambda settings: save_action(settings, action))
        except ValueError as exc:
            self.show_message(str(exc))
            return False
        except Exception as exc:
            self.show_message(f"Couldn't save the action: {exc}")
            return False
        self._secret_made = False
        self._is_new = False
        self._action = find_action(self._settings(), action.id) or action
        self._baseline = self.draft()
        self.saved.emit(self._action)
        return True

    def cancel(self) -> None:
        self.shortcut.cancel_capture()
        self._drop_unsaved_secret()
        self._action = None
        self._baseline = None
        self.cancelled.emit()

    def save_draft(self, *, ask: bool = False) -> bool:
        """Save an unsaved edit when Settings leaves the page (TransformsPanel's rule).

        Returns:
            False when the user chose to keep editing a draft that can't be saved.
        """
        if not self.has_unsaved_changes() or self.save() or not ask:
            return True
        answer = QMessageBox.question(
            self, "Unsaved action",
            f"{self.message.text()} Discard your changes to this action?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self.cancel()
        return True
