"""Settings → Meeting Mode → Fast judgments: the judge, its key, and the checks.

Picks who answers fast judgments (TypeSafe, OpenRouter, or one of the user's
own text models), says plainly when the key that powers them is missing, and
holds the master and per-feature switches. Testing a route runs on a worker
thread whose result comes back through a session-lived relay, so the worker
never holds or emits on the window.
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout

from services.settings import (
    SettingsKey,
    TypeSafeProvider,
    TypeSafeTextModel,
    resolve_typesafe_enabled,
    resolve_typesafe_text_model,
)
from services.typesafe import OPENROUTER_ROUTE as JEV_OPENROUTER_ROUTE
from services.typesafe import TEXT_MODEL_TIMEOUT_S
from services.typesafe import TYPESAFE_ROUTE as JEV_TYPESAFE_ROUTE
from services.typesafe import key_present as typesafe_key_present
from services.typesafe import route_from_settings as typesafe_route_from_settings
from services.typesafe import route_key as typesafe_route_key
from services.typesafe import text_model_route as typesafe_text_model_route
from services.typesafe import verify_key as typesafe_verify_key
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import Button, InfoTile, SettingTile, WrappedLabel
from ui_qt.widgets.buttons import compact_primary_button, neutral_button
from ui_qt.widgets.segmented_bar import SegmentedBar

logger = logging.getLogger(__name__)

#: The section's widgets the dialog also exposes by name, for deep links,
#: search reveals and tests (see settings_metadata.CONTROL_DESTINATIONS).
CONTROLS = (
    "typesafe_key_notice",
    "open_typesafe_key_btn",
    "typesafe_switch_route_btn",
    "typesafe_route_tile",
    "typesafe_route_bar",
    "typesafe_text_model_bar",
    "typesafe_route_test_btn",
    "typesafe_route_status",
    "typesafe_enabled_tile",
    "typesafe_enabled_check",
    "typesafe_topic_shift_tile",
    "typesafe_topic_shift_check",
    "typesafe_voice_commands_tile",
    "typesafe_voice_commands_check",
    "typesafe_feature_tiles",
    "typesafe_destination_label",
)

#: Fast judgments routes, in the order the Judge bar shows them.
_JUDGE_TITLES = {
    TypeSafeProvider.TYPESAFE: "TypeSafe",
    TypeSafeProvider.OPENROUTER: "OpenRouter",
    TypeSafeProvider.TEXT_MODEL: "Your text model",
}
#: Which text model answers on the text-model route, in the bar's order.
_JUDGE_TEXT_MODEL_TITLES = {
    TypeSafeTextModel.MEETING: "AI insights model",
    TypeSafeTextModel.CLEANUP: "AI cleanup model",
}


class _Relay(QObject):
    """Brings a route test's result to the Qt thread for whichever page exists then.

    It lives for the session, so the worker never holds or emits on a window.
    Holding one let the thread drop the last reference and delete the window
    off the Qt thread, and emitting on one raced it closing; both crash the
    process. Qt drops a deleted page's connections.
    """

    typesafe_route_verified = pyqtSignal(int, str, bool, str)


_relay: Optional[_Relay] = None
#: Request tokens, unique across windows because they share the relay.
_tokens = itertools.count(1)


def _thread_relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


class FastJudgmentsSection(QObject):
    """Builds the Fast judgments page and keeps its key notice honest.

    The dialog hands in what the page needs from the rest of the window:
    the settings as the window holds them, how to save one, the binder its
    plain switches load through, how to open API keys on a credential, and
    where the page's rail value goes.
    """

    def __init__(
        self,
        parent=None,
        *,
        settings_snapshot: Callable[[], dict],
        persist: Callable[[str, object], object],
        bindings,
        focus_api_keys: Callable[[Optional[str]], None],
        set_rail_value: Callable[[str], None],
        ensure_built: Callable[[], object] = lambda: None,
    ):
        super().__init__(parent)
        self._settings_snapshot = settings_snapshot
        self._persist = persist
        self._bindings = bindings
        self._focus_api_keys = focus_api_keys
        self._set_rail_value = set_rail_value
        self._ensure_built = ensure_built
        self._route_token = 0
        self._route_testing = False
        _thread_relay().typesafe_route_verified.connect(self._on_route_verified)

    def __getattr__(self, name):
        # Like the dialog's own controls, reaching one of the page's widgets
        # builds the page first.
        if name in CONTROLS:
            ensure = self.__dict__.get("_ensure_built")
            if ensure is not None:
                ensure()
                if name in self.__dict__:
                    return self.__dict__[name]
        raise AttributeError(name)

    def load(self, settings: dict) -> None:
        self.typesafe_enabled_check.setChecked(resolve_typesafe_enabled(settings))
        self.update_feature_tiles()

    def build(self, layout: QVBoxLayout, tile_group: Callable) -> None:
        # Every switch on this page is inert without a key, and the features
        # themselves degrade silently by design, so the page has to say so.
        self.typesafe_key_notice = InfoTile(
            "No TypeSafe API key",
            "",
            design_icon("info-warning.svg"),
        )
        self.typesafe_key_notice.setProperty("kind", "notice")
        self.open_typesafe_key_btn = compact_primary_button(Button("Add a key"))
        self.open_typesafe_key_btn.setObjectName("typesafeKeyNoticeLink")
        self.open_typesafe_key_btn.setToolTip(
            "Open API keys with the TypeSafe credential selected"
        )
        self.open_typesafe_key_btn.clicked.connect(
            lambda: self._focus_api_keys(self.selected_route().credential_env)
        )
        # The quickest fix when the other route already has a key: one click
        # moves Fast judgments onto it. Shown only in that case.
        self.typesafe_switch_route_btn = compact_primary_button(Button("Use OpenRouter"))
        self.typesafe_switch_route_btn.clicked.connect(self._switch_route)
        self.typesafe_switch_route_btn.hide()
        self.typesafe_key_notice.add_actions(
            self.typesafe_switch_route_btn, self.open_typesafe_key_btn
        )
        layout.addWidget(self.typesafe_key_notice)

        # Jev either way for the first two, the choice being whose key pays
        # and whether OpenRouter sits in between; or one of the user's own
        # text models, which can keep every judgment on this computer.
        self.typesafe_route_tile = InfoTile(
            "Judge",
            "",
            design_icon("world-blue.svg"),
        )
        self.typesafe_route_bar = SegmentedBar(
            [(_JUDGE_TITLES[route_id], "") for route_id in TypeSafeProvider.ALL]
        )
        self.typesafe_route_bar.setAccessibleName("Run fast judgments on")
        self.typesafe_route_bar.activated.connect(self._on_route_activated)
        self.typesafe_route_tile.add_body(self.typesafe_route_bar)
        # The text models are set up on the Models page; this only picks one.
        self.typesafe_text_model_bar = SegmentedBar(
            [(_JUDGE_TEXT_MODEL_TITLES[source], "") for source in TypeSafeTextModel.ALL],
            compact=True,
        )
        self.typesafe_text_model_bar.setAccessibleName("Text model that answers fast judgments")
        self.typesafe_text_model_bar.activated.connect(self._on_text_model_activated)
        self.typesafe_text_model_bar.hide()
        self.typesafe_route_tile.add_body(self.typesafe_text_model_bar)
        self.typesafe_route_test_btn = neutral_button(Button("Test"))
        self.typesafe_route_test_btn.setToolTip(
            "Send one tiny judgment (the word “ok”) over this connection"
        )
        self.typesafe_route_test_btn.clicked.connect(self.test_route)
        self.typesafe_route_tile.add_trailing(self.typesafe_route_test_btn)
        self.typesafe_route_status = WrappedLabel("")
        # Shares the API keys page's warning/success colours.
        self.typesafe_route_status.setObjectName("apiKeyStatus")
        self.typesafe_route_status.hide()
        self.typesafe_route_tile.add_body(self.typesafe_route_status)
        tile_group(layout, "", [self.typesafe_route_tile], columns=1)

        self.typesafe_enabled_tile = SettingTile(
            "Live checks",
            "Answers narrow yes/no questions about a minute of transcript: "
            "about 0.2 s with Jev, seconds with a text model. It never writes text.",
            design_icon("bolt-green.svg"),
        )
        self.typesafe_enabled_tile.add_title_badge("Experimental")
        self.typesafe_enabled_check = self.typesafe_enabled_tile.checkbox
        self.typesafe_enabled_check.setObjectName("typesafeEnabledCheck")
        self.typesafe_enabled_check.toggled.connect(self._on_enabled_changed)

        self.typesafe_topic_shift_tile = SettingTile(
            "Semantic topic changes",
            "Fires early checkpoints on a judged topic change instead of word "
            "overlap, and falls back to word overlap if no answer arrives.",
            design_icon("stack-purple.svg"),
        )
        self.typesafe_topic_shift_tile.add_title_badge("Experimental")
        self.typesafe_topic_shift_check = self.typesafe_topic_shift_tile.checkbox
        self.typesafe_topic_shift_check.setObjectName("typesafeTopicShiftCheck")
        self._bindings.checkbox(
            self.typesafe_topic_shift_check,
            SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED,
            SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED.read,
        )

        self.typesafe_voice_commands_tile = SettingTile(
            "Spoken instructions",
            "Say “Note taker, mark that as a decision”, “…add an action item” or "
            "“…new topic: budget”. Only segments naming the assistant are judged. "
            "“Replace X with Y” applies a reversible transcript correction.",
            design_icon("stack-slate.svg"),
        )
        self.typesafe_voice_commands_tile.add_title_badge("Experimental")
        self.typesafe_voice_commands_check = (
            self.typesafe_voice_commands_tile.checkbox
        )
        self.typesafe_voice_commands_check.setObjectName(
            "typesafeVoiceCommandsCheck"
        )
        self._bindings.checkbox(
            self.typesafe_voice_commands_check,
            SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED,
            SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED.read,
        )
        self.typesafe_feature_tiles = {}
        for feature, key, title, description in (
            ("citations", SettingsKey.TYPESAFE_CITATIONS_ENABLED, "Advisory citation checks",
             "Gives the judge generated claims and their cited excerpts. "
             "Flags weak evidence without changing the claim."),
            ("semantic_search", SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED, "Semantic history search",
             "Gives the judge your search and shortlisted past excerpts to rank by meaning: "
             "only from AI-insight meetings unless it runs on this computer. "
             "Keyword search stays available."),
            ("question_radar", SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED, "Open questions radar",
             "Gives the judge a minute of transcript and tracked questions "
             "to find unanswered questions and suggest answers."),
            ("highlights", SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED, "Live highlight pulses",
             "Gives the judge a minute of transcript to mark decisions, disagreements, "
             "dated commitments, numbers and takeaways. Click a pulse to play that moment."),
        ):
            tile = SettingTile(title, description, design_icon("bolt-green.svg"))
            self._bindings.checkbox(
                tile.checkbox,
                key,
                lambda settings, key=key: settings.get(key, False) is True,
            )
            self.typesafe_feature_tiles[feature] = tile
        _caption, self.typesafe_destination_label = tile_group(
            layout,
            "Features",
            [
                self.typesafe_enabled_tile,
                self.typesafe_topic_shift_tile,
                self.typesafe_voice_commands_tile,
                *self.typesafe_feature_tiles.values(),
            ],
            columns=3,
            intro=self._destination_copy(JEV_TYPESAFE_ROUTE),
        )

    def _on_enabled_changed(self, checked: bool) -> None:
        self._persist(SettingsKey.TYPESAFE_ENABLED, bool(checked))
        self.update_feature_tiles()

    def selected_route(self):
        """The Fast judgments route Settings name now: TypeSafe unless another was chosen."""
        return typesafe_route_from_settings(self._settings_snapshot())

    def _key_present(self, route=None) -> bool:
        """Whether ``route``'s key (default: the chosen route's) resolves, never raising into the UI."""
        try:
            return typesafe_key_present(route=route or self.selected_route())
        except Exception:
            logger.exception("TypeSafe key lookup failed")
            return False

    @staticmethod
    def _other_route(route):
        """The other way to Jev, whose key can stand in; None for a text model."""
        if route.profile is not None:
            return None
        return JEV_OPENROUTER_ROUTE if route is JEV_TYPESAFE_ROUTE else JEV_TYPESAFE_ROUTE

    @staticmethod
    def _destination_copy(route) -> str:
        """Who receives excerpts over ``route``; OpenRouter is a hop, not the model."""
        if route.is_local:
            return (
                f"Transcript excerpts stay on this computer: {route.name} answers "
                "here, even in meetings with AI insights off."
            )
        if route.profile is not None:
            return (
                f"Transcript excerpts go to {route.name} ({route.host}) only "
                "while AI insights are on for the meeting."
            )
        if route is JEV_TYPESAFE_ROUTE:
            return (
                f"Transcript excerpts go to TypeSafe ({route.host}) only "
                "while AI insights are on for the meeting."
            )
        return (
            f"Transcript excerpts go to {route.name} ({route.host}), which "
            "passes them to TypeSafe, only while AI insights are on for the "
            "meeting."
        )

    def _on_route_activated(self, index: int) -> None:
        route_id = TypeSafeProvider.ALL[index]
        if route_id == self.selected_route().id:
            return
        self._persist(SettingsKey.TYPESAFE_PROVIDER, route_id)
        # A result for the other route would now describe the wrong thing.
        self.typesafe_route_status.hide()
        self.update_feature_tiles()

    def _on_text_model_activated(self, index: int) -> None:
        source = TypeSafeTextModel.ALL[index]
        if source == resolve_typesafe_text_model(self._settings_snapshot()):
            return
        self._persist(SettingsKey.TYPESAFE_TEXT_MODEL, source)
        self.typesafe_route_status.hide()
        self.update_feature_tiles()

    def _switch_route(self) -> None:
        other = self._other_route(self.selected_route())
        if other is None:
            return
        self.typesafe_route_bar.setCurrentIndex(TypeSafeProvider.ALL.index(other.id))
        self._on_route_activated(self.typesafe_route_bar.currentIndex())

    @staticmethod
    def _text_model_label(route) -> str:
        # OpenRouter ids carry the vendor; the profile already names the route.
        model = (route.model or "").rsplit("/", 1)[-1]
        return f"{route.name} · {model}" if model else route.name

    def _render_route(self) -> None:
        """Show the chosen route, each route's key state, and where excerpts go."""
        # The destination line is the page's last widget, so a refresh during
        # construction waits for the whole page.
        if "typesafe_destination_label" not in self.__dict__:
            return
        settings = self._settings_snapshot()
        route = self.selected_route()
        text_route = typesafe_text_model_route(settings)
        self.typesafe_route_bar.setCurrentIndex(TypeSafeProvider.ALL.index(route.id))
        routes = {**{r.id: r for r in (JEV_TYPESAFE_ROUTE, JEV_OPENROUTER_ROUTE)},
                  TypeSafeProvider.TEXT_MODEL: text_route}
        for index, candidate in enumerate(routes[route_id] for route_id in TypeSafeProvider.ALL):
            state = "key set" if self._key_present(candidate) else "no key"
            detail = (
                f"Direct · {state}" if candidate is JEV_TYPESAFE_ROUTE else
                f"Same model · {state}" if candidate is JEV_OPENROUTER_ROUTE else
                f"{candidate.name} · on this PC" if candidate.is_local else
                f"{candidate.name} · {state}"
            )
            self.typesafe_route_bar.set_detail(index, detail)
        self.typesafe_text_model_bar.setVisible(route.profile is not None)
        self.typesafe_text_model_bar.setCurrentIndex(
            TypeSafeTextModel.ALL.index(resolve_typesafe_text_model(settings)))
        for index, source in enumerate(TypeSafeTextModel.ALL):
            candidate = typesafe_text_model_route({**settings, SettingsKey.TYPESAFE_TEXT_MODEL: source})
            self.typesafe_text_model_bar.set_detail(index, self._text_model_label(candidate))
        if route is JEV_TYPESAFE_ROUTE:
            description = (
                f"Answers come straight from TypeSafe ({route.host}) and are "
                "billed to your TypeSafe account."
            )
        elif route is JEV_OPENROUTER_ROUTE:
            description = (
                f"The same Jev model, reached through {route.name} ({route.host}) "
                f"and billed to your {route.name} credits. No TypeSafe account needed."
            )
        else:
            where = "on this computer" if route.is_local else f"at {route.host}"
            description = (
                f"{self._text_model_label(route)} answers {where}, as set under AI "
                "cleanup or Meeting Mode → Intelligence. Slower than Jev and not benchmarked like it: a "
                f"judgment that takes over {TEXT_MODEL_TIMEOUT_S:.0f} s is skipped."
            )
        self.typesafe_route_tile.set_description(description)
        self.typesafe_destination_label.setText(self._destination_copy(route))
        self.typesafe_route_test_btn.setEnabled(not self._route_testing)

    def test_route(self) -> None:
        if self._route_testing:
            return
        route = self.selected_route()
        key = typesafe_route_key(route)
        if not key:
            self._show_route_result(
                False, f"No {route.key_label} to test. Add one under API keys → {route.name}."
            )
            return
        self._route_testing = True
        self.typesafe_route_test_btn.setEnabled(False)
        self._show_route_result(None, f"Testing {route.name}…")
        self._route_token = token = next(_tokens)
        # Bound here: the worker must not reach back into the window.
        verify = typesafe_verify_key
        done = _thread_relay().typesafe_route_verified

        def worker():
            ok, detail = verify(key, route=route)
            done.emit(token, route.id, ok, detail)

        threading.Thread(target=worker, daemon=True, name="jev-route-verify").start()

    def _on_route_verified(
        self, token: int, route_id: str, ok: bool, detail: str
    ) -> None:
        if token != self._route_token:
            return
        self._route_testing = False
        self.typesafe_route_test_btn.setEnabled(True)
        # The person switched routes while this one was being tested.
        if route_id != self.selected_route().id:
            return
        self._show_route_result(ok, detail)

    def _show_route_result(self, ok: Optional[bool], text: str) -> None:
        self.typesafe_route_status.setText(text)
        set_style_property(self.typesafe_route_status, "tone", "success" if ok else "warning")
        self.typesafe_route_status.show()

    def render_key_state(self) -> None:
        """Say plainly when a switch is on but the key that powers it is missing.

        The features themselves are built to degrade to "no judgment", which
        is right at runtime and wrong in Settings: without this the page looks
        identical whether or not anything will ever run.
        """
        # Rail refreshes can run before the fast-judgments page is built.
        if "typesafe_key_notice" not in self.__dict__:
            return
        self._render_route()
        route = self.selected_route()
        other = self._other_route(route)
        present = self._key_present(route)
        other_present = other is not None and not present and self._key_present(other)
        master_on = self.typesafe_enabled_check.isChecked()
        self.typesafe_key_notice.setVisible(not present)
        self.typesafe_switch_route_btn.setVisible(other_present)
        # One blue action per banner: switching routes leads when it can.
        set_style_property(
            self.open_typesafe_key_btn, "tone", "neutral" if other_present else "primary"
        )
        if other is not None:
            self.typesafe_switch_route_btn.setText(f"Use {other.name}")
        self.open_typesafe_key_btn.setToolTip(
            f"Open API keys with the {route.name} credential selected"
        )
        if present:
            return
        self.typesafe_key_notice.title_label.setText(f"No {route.key_label}")
        situation = (
            "Nothing on this page runs without one, and these checks fail "
            "quietly by design — no meeting will warn you."
            if master_on else
            "Fast judgments are off and no key is set. Turning anything on "
            "below has no effect until there is one."
        )
        remedy = (
            f"Your {other.key_label} can run the same model instead, or add a "
            f"key under API keys → {route.name}."
            if other_present else
            f"Add a key under API keys → {route.name}, or set "
            f"{route.credential_env}."
        )
        self.typesafe_key_notice.set_description(f"{situation} {remedy}")

    def update_feature_tiles(self) -> None:
        """Feature switches only mean something while the master switch is on."""
        enabled = self.typesafe_enabled_check.isChecked()
        for tile in (
            self.typesafe_topic_shift_tile,
            self.typesafe_voice_commands_tile,
            *self.typesafe_feature_tiles.values(),
        ):
            tile.setEnabled(enabled)
        self.render_key_state()
        self._set_rail_value(self.rail_value())

    def rail_value(self) -> str:
        settings = self._settings_snapshot()
        if not resolve_typesafe_enabled(settings):
            return "Off"
        if not self._key_present():
            return "No key"
        keys = (SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED,
                SettingsKey.TYPESAFE_CITATIONS_ENABLED, SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED,
                SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED, SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED)
        active = sum(key.read(settings) for key in keys)
        value = "On · no features" if active == 0 else f"On · {active}"
        route = typesafe_route_from_settings(settings)
        return value if route is JEV_TYPESAFE_ROUTE else f"{value} · {route.name}"

