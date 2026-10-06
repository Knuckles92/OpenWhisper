"""Settings → Personalize → Dictionary: words dictation should spell your way.

Settings imports this module when it registers its pages, before the page is
built, so module-level imports stay light; ``build`` imports the widgets.
Dictionary edits go straight to the settings file through the dictionary's
own mutators, so a word is saved the moment it is added.
"""
import logging

from PyQt6.QtCore import QEvent, QObject

logger = logging.getLogger(__name__)

TITLE = "Dictionary"
SUBTITLE = "Names, jargon, and spellings OpenWhisper should get right every time."

_GENERIC_ENGINE_LINE = (
    "Engines that take hints also listen for your words. Every engine fixes "
    "spellings after recognition."
)
_STEER_OFF_LINE = (
    "Off. Your dictionary still fixes spellings after recognition and in AI cleanup."
)
_STEER_OFF_NO_CLEANUP_LINE = "Off. Only Sounds like spellings are fixed while AI cleanup is off."
_NOT_FIXED_HINT = "Without a Sounds like spelling it won't be fixed while AI cleanup is off."
_LEARN_LINE = (
    "When you fix a dictated word the same way twice, it's added here. Only "
    "the words you just dictated are compared."
)
_LEARN_GATE_LINE = "Off · needs Read text near the cursor"

SEARCH_FIELDS = [
    (
        "dictionary_composer_tile",
        "Add a word",
        "A name or term, and what it sounds like when dictation gets it wrong.",
    ),
    (
        "dictionary_library_tile",
        "Your words",
        "Starred words come first when an engine takes only a few hints.",
    ),
    ("dictionary_steer_tile", "Steer the speech model", _GENERIC_ENGINE_LINE),
    ("dictionary_learn_tile", "Learn from my corrections", _LEARN_LINE),
]
CONTROL_ATTRS = (
    "dictionary_composer_tile",
    "dictionary_term_edit",
    "dictionary_heard_edit",
    "dictionary_add_button",
    "dictionary_cancel_button",
    "dictionary_message",
    "dictionary_new_tile",
    "dictionary_rules_tile",
    "dictionary_library_tile",
    "dictionary_library",
    "dictionary_steer_tile",
    "dictionary_steer_switch",
    "dictionary_learn_tile",
    "dictionary_learn_switch",
    "dictionary_learn_gate_tile",
    "dictionary_cleanup_gate_tile",
)

_ENGINE_NAMES = {"local_whisper": "Whisper", "api": "OpenAI"}

#: Returns the app's current transcription backend, so the page can say
#: whether a paired host's engine takes hints. Without it the page goes by
#: the selected engine alone.
_backend_provider = None


def set_backend_provider(provider) -> None:
    """Let the page read the live backend; ``provider()`` runs on the Qt thread."""
    global _backend_provider
    _backend_provider = provider


def _manager():
    # The dialog module's manager, so a dialog built on another store (tests,
    # a profile) reads and writes the same file.
    from ui_qt.dialogs import settings_dialog

    return settings_dialog.settings_manager


def _current_backend():
    provider = _backend_provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        logger.debug("The current backend is unavailable", exc_info=True)
        return None


def _engine_support(settings: dict, backend=None) -> tuple[str, str, str]:
    """``(support, name, destination)`` for the dictation engine in use.

    ``support`` is "" (and the name may be) when only a paired host that
    isn't connected could say whether it takes hints.
    """
    from services.local_asr.catalog import BACKENDS
    from services.recognition_context import engine_support
    from services.settings import SettingsKey, setting_value

    engine = setting_value(SettingsKey.SELECTED_MODEL, settings)
    support = engine_support(engine)
    name = _ENGINE_NAMES.get(engine) or BACKENDS.get(engine, "")
    destination = "OpenAI" if engine == "api" else ""
    if engine == "remote" and getattr(backend, "is_remote", False) is True and backend.is_available():
        support = backend.recognition_support
        name = backend.name
        destination = backend.host_name
    return (support, name, destination) if name else ("", "", "")


def _cleanup_on(settings: dict) -> bool:
    from services import cleanup_prompts
    from services.cleanup_prompts import CleanupLevel

    return cleanup_prompts.resolve_level(settings) != CleanupLevel.NONE


def cannot_steer(settings: dict, backend=None) -> bool:
    """Whether the engine in use is known to ignore dictionary hints."""
    from services.recognition_context import SUPPORT_AFTER

    return _engine_support(settings, backend)[0] == SUPPORT_AFTER


def sounds_like_only(settings: dict, backend=None) -> bool:
    """Whether only Sounds like spellings fix words now.

    True when AI cleanup is off and the words don't reach the speech model,
    because the engine can't take them or steering is off.
    """
    from services.recognition_context import SUPPORT_MODEL
    from services.settings import resolve_dictionary_steer_recognition

    support = _engine_support(settings, backend)[0]
    if not support or _cleanup_on(settings):
        return False
    return support != SUPPORT_MODEL or not resolve_dictionary_steer_recognition(settings)


def engine_line(settings: dict, backend=None) -> str:
    """What the dictionary does with the dictation engine in use."""
    from services.recognition_context import SUPPORT_MODEL

    support, name, destination = _engine_support(settings, backend)
    if not support:
        return _GENERIC_ENGINE_LINE
    if support != SUPPORT_MODEL:
        if _cleanup_on(settings):
            return (f"Your engine ({name}) can't be steered. AI cleanup and Sounds "
                    "like spellings fix your words after recognition.")
        return (f"Your engine ({name}) can't be steered, and AI cleanup is off, so "
                "only Sounds like spellings are fixed.")
    line = f"Your engine ({name}) also steers the speech model toward your words."
    if destination:
        line += f" They're sent to {destination} with your audio."
    return line


def rail_value(settings: dict) -> str:
    from services.dictionary import summary

    return summary(settings)


def build(dialog, layout) -> None:
    dialog._dictionary_page = _DictionaryPage(dialog, layout)


def load(dialog, settings: dict) -> None:
    page = dialog.__dict__.get("_dictionary_page")
    if page is not None:
        page.show_settings(settings)


def refresh(dialog) -> None:
    page = dialog.__dict__.get("_dictionary_page")
    if page is not None:
        page.show_settings(dialog._settings_snapshot())


def basic_detail(settings: dict) -> str:
    """The Basic Dictionary row's line: the count, and what fixes words now."""
    from services.dictionary import load_dictionary, summary

    limited = sounds_like_only(settings, _current_backend())
    if load_dictionary(settings):
        count = summary(settings)
        return f"{count} · Only Sounds like spellings are fixed while AI cleanup is off." if limited else count
    if limited:
        return "Names and terms to get right. With AI cleanup off, each needs a Sounds like spelling."
    return "Names and terms dictation should always get right."


def basic_rows(page, group) -> None:
    from ui_qt.widgets.buttons import Button, fit_compact_button, neutral_button

    button = neutral_button(Button("Add word"))
    button.setObjectName("basicDictionaryAddButton")
    fit_compact_button(button)
    detail = page._row(group, "Dictionary", "", button)
    button.setAccessibleName("Add word to dictionary")
    button.clicked.connect(lambda: open_composer(page.dialog))
    page.add_refresh_hook(lambda settings: detail.setText(basic_detail(settings)))


def open_composer(dialog) -> None:
    """Show the Dictionary page with the word field focused."""
    from ui_qt.dialogs.settings_destinations import DICTIONARY

    dialog.select_destination(DICTIONARY)
    dialog._reveal(dialog.dictionary_term_edit)


def _add_action(tile, button) -> None:
    # Under the description rather than beside it, where a narrow window or a
    # large font would push it past the tile's edge.
    from PyQt6.QtWidgets import QHBoxLayout

    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(button)
    row.addStretch()
    tile.add_body_layout(row)


class _DictionaryPage(QObject):
    """The page's widgets and what they do; one per Settings window.

    Owned by the page widget, whose Show events it watches: text reading is
    switched on another page, and coming back here has to show the learning
    row that unlocked.
    """

    def __init__(self, dialog, layout):
        from PyQt6.QtWidgets import QLabel, QLineEdit

        from services.dictionary import MAX_TERM_CHARS
        from ui_qt.utils.icons import design_icon
        from ui_qt.widgets.buttons import Button, compact_primary_button, fit_compact_button, neutral_button
        from ui_qt.widgets.dictionary_library import DictionaryLibrary, Reflow, action_group
        from ui_qt.widgets.setting_tile import InfoTile, SettingTile
        from ui_qt.widgets.wrapped_label import WrappedLabel

        super().__init__(layout.parentWidget())
        self.dialog = dialog
        self._editing = ""
        self._settings = {}
        self._showing = False

        # Shown only while AI cleanup is off on an engine that can't take
        # your words, when Sounds like spellings are all that's fixed.
        self.cleanup_gate = dialog.cleanup_gate_tile(
            "Only Sounds like spellings get fixed. AI cleanup uses your words too.",
            needed=lambda: cannot_steer(dialog._settings_snapshot(), _current_backend()),
        )
        self.cleanup_gate.setProperty("tileId", "dictionaryCleanupGate")
        # After the dialog's own handler has turned cleanup on.
        dialog._cleanup_gates[-1][1].clicked.connect(
            lambda: self.show_settings(dialog._settings_snapshot())
        )
        layout.addWidget(self.cleanup_gate)

        (attr, title, description) = SEARCH_FIELDS[0]
        composer = InfoTile(title, description, design_icon("plus-blue.svg"))
        composer.setProperty("tileId", "dictionaryComposer")
        self.term_edit = QLineEdit()
        self.term_edit.setObjectName("dictionaryTermInput")
        self.term_edit.setPlaceholderText("Word or name")
        self.term_edit.setMaxLength(MAX_TERM_CHARS)
        self.term_edit.setAccessibleName("Word or name")
        self.term_edit.returnPressed.connect(self.submit)
        self.term_edit.textEdited.connect(lambda _text: self.say(""))
        composer.add_body(self.term_edit)
        self.heard_edit = QLineEdit()
        self.heard_edit.setObjectName("dictionaryHeardInput")
        self.heard_edit.setPlaceholderText("Sounds like (optional)")
        self.heard_edit.setToolTip(
            "How dictation sometimes writes it. Separate several with commas; "
            "each is replaced with your word."
        )
        self.heard_edit.setAccessibleName("Sounds like")
        self.heard_edit.returnPressed.connect(self.submit)
        self.heard_edit.textEdited.connect(lambda _text: self.say(""))
        self.cancel_button = neutral_button(Button("Cancel"))
        self.cancel_button.setObjectName("dictionaryCancelButton")
        fit_compact_button(self.cancel_button)
        self.cancel_button.clicked.connect(self.cancel_edit)
        self.cancel_button.hide()
        self.add_button = compact_primary_button(Button("Add"))
        self.add_button.setObjectName("dictionaryAddButton")
        fit_compact_button(self.add_button)
        self.add_button.clicked.connect(self.submit)
        composer.add_body(Reflow(
            self.heard_edit, action_group(self.cancel_button, self.add_button), lead_width=220,
        ))
        self.message = WrappedLabel("")
        self.message.setObjectName("dictionaryMessage")
        self.message.hide()
        composer.add_body(self.message)
        self.composer = composer
        dialog._tile_group(layout, "", [composer], columns=1)

        self.new_tile = InfoTile(
            "New from your corrections", "", design_icon("wand-purple.svg")
        )
        self.new_tile.setProperty("tileId", "dictionaryNew")
        keep = neutral_button(Button("Keep all"))
        keep.setObjectName("dictionaryKeepNewButton")
        keep.setToolTip("Keep every new word and clear the New badges")
        fit_compact_button(keep)
        keep.clicked.connect(self.keep_new)
        _add_action(self.new_tile, keep)
        self.new_tile.hide()
        layout.addWidget(self.new_tile)

        self.rules_tile = InfoTile(
            "Spellings in Learned rules", "", design_icon("stack-purple.svg")
        )
        self.rules_tile.setProperty("tileId", "dictionaryRules")
        self.move_button = neutral_button(Button("Move here"))
        self.move_button.setObjectName("dictionaryMoveRulesButton")
        fit_compact_button(self.move_button)
        self.move_button.clicked.connect(self.move_rules)
        _add_action(self.rules_tile, self.move_button)
        self.rules_tile.hide()
        layout.addWidget(self.rules_tile)

        (_attr, title, description) = SEARCH_FIELDS[1]
        self.library_tile = InfoTile(title, description, design_icon("book-blue.svg"))
        self.library_tile.setProperty("tileId", "dictionaryLibrary")
        self.count = QLabel("")
        self.count.setObjectName("dictionaryCount")
        self.library_tile.add_trailing(self.count)
        self.library = DictionaryLibrary(focus_when_empty=self.term_edit)
        self.library.star_toggled.connect(self.star)
        self.library.edit_requested.connect(self.begin_edit)
        self.library.remove_requested.connect(self.remove)
        self.library_tile.add_body(self.library)
        dialog._tile_group(layout, "", [self.library_tile], columns=1)

        (_attr, title, description) = SEARCH_FIELDS[2]
        self.steer_tile = SettingTile(title, description, design_icon("microphone-blue.svg"))
        self.steer_tile.setProperty("tileId", "dictionarySteer")
        self.steer_switch = self.steer_tile.checkbox
        self.steer_switch.toggled.connect(self.set_steering)
        (_attr, title, description) = SEARCH_FIELDS[3]
        self.learn_tile = SettingTile(title, description, design_icon("wand-purple.svg"))
        self.learn_tile.setProperty("tileId", "dictionaryLearn")
        self.learn_switch = self.learn_tile.checkbox
        self.learn_switch.toggled.connect(self.set_learning)
        self.learn_gate_tile = InfoTile(title, _LEARN_GATE_LINE, design_icon("wand-purple.svg"))
        self.learn_gate_tile.setProperty("tileId", "dictionaryLearnGate")
        turn_on = neutral_button(Button("Open Apps && styles"))
        turn_on.setObjectName("dictionaryLearnTurnOnButton")
        turn_on.setToolTip("Open Apps & styles to turn on Read text near the cursor")
        fit_compact_button(turn_on)
        turn_on.clicked.connect(self.open_text_reading)
        _add_action(self.learn_gate_tile, turn_on)
        self.learn_gate_tile.hide()
        dialog._tile_group(layout, "How your dictionary is used", [self.steer_tile], columns=1)
        dialog._tile_group(layout, "", [self.learn_tile, self.learn_gate_tile], columns=1)

        for name, widget in (
            ("dictionary_composer_tile", composer),
            ("dictionary_term_edit", self.term_edit),
            ("dictionary_heard_edit", self.heard_edit),
            ("dictionary_add_button", self.add_button),
            ("dictionary_cancel_button", self.cancel_button),
            ("dictionary_message", self.message),
            ("dictionary_new_tile", self.new_tile),
            ("dictionary_rules_tile", self.rules_tile),
            ("dictionary_library_tile", self.library_tile),
            ("dictionary_library", self.library),
            ("dictionary_steer_tile", self.steer_tile),
            ("dictionary_steer_switch", self.steer_switch),
            ("dictionary_learn_tile", self.learn_tile),
            ("dictionary_learn_switch", self.learn_switch),
            ("dictionary_learn_gate_tile", self.learn_gate_tile),
            ("dictionary_cleanup_gate_tile", self.cleanup_gate),
        ):
            setattr(dialog, name, widget)
        layout.parentWidget().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            self.show_settings(self.dialog._settings_snapshot())
        return False

    # ---- showing the saved state -------------------------------------------

    def show_settings(self, settings: dict) -> None:
        self._showing = True
        try:
            self._show(settings)
        finally:
            self._showing = False

    def _show(self, settings: dict) -> None:
        from config import config
        from services.dictionary import load_dictionary, spelling_rule, spelling_rules
        from services.recognition_context import SUPPORT_AFTER, SUPPORT_MODEL
        from services.settings import (
            resolve_app_context_read_text,
            resolve_dictionary_learn_enabled,
            resolve_dictionary_steer_recognition,
        )

        self._settings = settings
        backend = _current_backend()
        support, name, _destination = _engine_support(settings, backend)
        steering = resolve_dictionary_steer_recognition(settings)
        if name:
            self.cleanup_gate.set_description(
                f"{name} can't listen for your words, so only Sounds like spellings "
                "get fixed. AI cleanup uses your words too."
            )
        self.dialog.refresh_cleanup_gates()
        terms = load_dictionary(settings)
        self.library.set_terms(terms)
        self.count.setText(f"{len(terms)} / {config.MAX_DICTIONARY_TERMS}")
        self.count.setVisible(bool(terms))
        fresh = sum(term.new for term in terms)
        self.new_tile.set_description(
            f"{fresh} new word{'' if fresh == 1 else 's'}. Undo any you don't want."
        )
        self.new_tile.setVisible(bool(fresh))
        moving = spelling_rules(settings)
        rules = len(moving)
        # A moved word works without AI cleanup only through the speech
        # model or its Sounds like spelling.
        if (support == SUPPORT_MODEL and steering) or all(spelling_rule(rule)[1] for rule in moving):
            reason = ("As dictionary words they also work when AI cleanup is off." if rules != 1
                      else "As a dictionary word it also works when AI cleanup is off.")
        else:
            reason = ("They can live here with your other words." if rules != 1
                      else "It can live here with your other words.")
        self.rules_tile.set_description(
            f"{rules} of your Learned rules only spell a word. {reason}" if rules != 1
            else f"One of your Learned rules only spells a word. {reason}"
        )
        self.move_button.setText("Move them here" if rules != 1 else "Move it here")
        self.rules_tile.setVisible(bool(rules))
        self.steer_switch.setChecked(steering)
        # The saved choice stays for engines that take hints.
        blocked = support == SUPPORT_AFTER
        self.steer_tile.setEnabled(not blocked)
        if steering or blocked:
            self.steer_tile.set_description(engine_line(settings, backend))
        else:
            self.steer_tile.set_description(
                _STEER_OFF_LINE if _cleanup_on(settings) else _STEER_OFF_NO_CLEANUP_LINE
            )
        reading = resolve_app_context_read_text(settings)
        self.learn_switch.setChecked(resolve_dictionary_learn_enabled(settings))
        self.learn_tile.setVisible(reading)
        self.learn_gate_tile.setVisible(not reading)
        if self._editing and not any(term.id == self._editing for term in terms):
            self.cancel_edit()

    def reload(self) -> None:
        self.show_settings(self.dialog._settings_snapshot())
        self.dialog.notify_changed("dictionary")

    def say(self, text: str, *, error: bool = False) -> None:
        from ui_qt.utils.restyle import set_style_property

        self.message.setText(text)
        set_style_property(self.message, "tone", "error" if error else "ok")
        self.message.setVisible(bool(text))

    def _mutate(self, change, failure: str):
        """Run a dictionary mutator: ``(its result, True)``, or ``(None, False)``
        once the user has been told why it failed."""
        from services.dictionary import DictionaryError

        try:
            return _manager().mutate_settings(change), True
        except DictionaryError as exc:
            self.say(str(exc), error=True)
        except Exception:
            logger.warning("Couldn't save the dictionary", exc_info=True)
            self.say(failure, error=True)
        return None, False

    # ---- actions -----------------------------------------------------------

    def submit(self) -> None:
        from services.dictionary import add_term, parse_heard, update_term

        term = self.term_edit.text()
        heard = parse_heard(self.heard_edit.text())
        editing = self._editing
        if editing:
            saved, ok = self._mutate(
                lambda settings: update_term(settings, editing, term=term, heard=heard),
                "Couldn't save the change. Try again.",
            )
        else:
            saved, ok = self._mutate(
                lambda settings: add_term(settings, term, heard=heard),
                "Couldn't add the word. Try again.",
            )
        if not ok:
            return
        self._end_edit()
        self.term_edit.clear()
        self.heard_edit.clear()
        self.term_edit.setFocus()
        self.reload()
        message = f"Saved “{saved.term}”." if editing else f"Added “{saved.term}”."
        if not saved.heard and sounds_like_only(self._settings, _current_backend()):
            message += f" {_NOT_FIXED_HINT}"
        self.say(message)

    def begin_edit(self, term_id: str) -> None:
        from services.dictionary import load_dictionary

        term = next((term for term in load_dictionary(self._settings) if term.id == term_id), None)
        if term is None:
            return
        self._editing = term_id
        self.composer.title_label.setText("Edit a word")
        self.term_edit.setText(term.term)
        self.heard_edit.setText(", ".join(term.heard))
        self.add_button.setText("Save")
        self.cancel_button.show()
        self.say("")
        self.dialog._reveal(self.term_edit)

    def cancel_edit(self) -> None:
        self._end_edit()
        self.term_edit.clear()
        self.heard_edit.clear()
        self.say("")

    def _end_edit(self) -> None:
        self._editing = ""
        self.composer.title_label.setText(SEARCH_FIELDS[0][1])
        self.add_button.setText("Add")
        self.cancel_button.hide()

    def star(self, term_id: str, starred: bool) -> None:
        from services.dictionary import set_starred

        self._mutate(
            lambda settings: set_starred(settings, term_id, starred),
            "Couldn't save the star. Try again.",
        )
        self.reload()

    def remove(self, term_id: str) -> None:
        from services.dictionary import delete_term, undo_learned

        row = self.library.row(term_id)
        forget = row is not None and row.term.learned and row.term.new
        removed, ok = self._mutate(
            lambda settings: (undo_learned if forget else delete_term)(settings, term_id),
            "Couldn't remove the word. Try again.",
        )
        if not ok:
            return
        if self._editing == term_id:
            self.cancel_edit()
        self.reload()
        if removed is not None:
            self.say(f"Forgot “{removed.term}”." if forget else f"Removed “{removed.term}”.")

    def keep_new(self) -> None:
        from services.dictionary import clear_new

        _count, ok = self._mutate(clear_new, "Couldn't save that. Try again.")
        if ok:
            self.reload()

    def move_rules(self) -> None:
        from services.dictionary import move_spelling_rules
        from services.settings import resolve_transcript_cleanup_rules

        moved, ok = self._mutate(move_spelling_rules, "Couldn't move the rules. Try again.")
        if not ok:
            return
        rules_list = self.dialog.__dict__.get("cleanup_rules_list")
        if rules_list is not None:
            # The Learned rules page saves its list as shown, so it must show
            # the rules that are left.
            rules_list.clear()
            rules_list.addItems(resolve_transcript_cleanup_rules(self.dialog._settings_snapshot()))
            self.dialog._update_cleanup_rule_controls()
        self.reload()
        if moved:
            self.say(f"Moved {moved} spelling rule{'' if moved == 1 else 's'} here.")

    def set_steering(self, checked: bool) -> None:
        from services.settings import SettingsKey

        if not self._showing and self.dialog._persist(
            SettingsKey.DICTIONARY_STEER_RECOGNITION, bool(checked)
        ):
            self.reload()

    def set_learning(self, checked: bool) -> None:
        from services.settings import SettingsKey

        if not self._showing and self.dialog._persist(
            SettingsKey.DICTIONARY_LEARN_ENABLED, bool(checked)
        ):
            self.reload()

    def open_text_reading(self) -> None:
        from ui_qt.dialogs.settings_destinations import STYLES

        self.dialog.select_destination(STYLES)
