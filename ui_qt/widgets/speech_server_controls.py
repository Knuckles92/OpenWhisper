"""The API backend's Server and Model fields.

Server lists OpenAI, the hosted and self-hosted presets and the Custom slot,
then an "Edit server…" action for the selected self-hosted one. Picking the
Custom slot before it has an address, or the action, opens the editor and
restores the current selection, so backing out changes nothing.

Model lists OpenAI's curated models, or what the selected server's catalog
reports beside the model already chosen. A catalog is fetched off the Qt
thread when the server is picked and kept for the session.
"""
import itertools
import logging
import threading
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from services import speech_endpoints
from services.settings import api_model_label, settings_manager
from services.speech_endpoints import CUSTOM_ENDPOINT_ID, ENDPOINTS, SpeechEndpoint
from ui_qt.widgets.engine_field import engine_combo, engine_field

logger = logging.getLogger(__name__)

#: Combo data for the trailing edit action; outside the endpoint-id space.
_EDIT_OPTION = "__edit__"


class _CatalogRelay(QObject):
    """Brings catalog results to the Qt thread for whichever fields exist then.

    It lives for the session, so a worker never holds or emits on a widget.
    """

    models_done = pyqtSignal(int, object, object)


_relay: Optional[_CatalogRelay] = None
_tokens = itertools.count(1)
#: Catalog per (endpoint id, address), shared by both tabs' fields.
_catalogs: Dict[Tuple[str, Optional[str]], List[str]] = {}


def _catalog_relay() -> _CatalogRelay:
    global _relay
    if _relay is None:
        _relay = _CatalogRelay()
    return _relay


def _catalog_key(endpoint: SpeechEndpoint) -> Tuple[str, Optional[str]]:
    return endpoint.id, endpoint.base_url


class SpeechServerControls(QWidget):
    """Server and Model fields that save their choice as it is made."""

    #: The server or model changed.
    changed = pyqtSignal()
    help_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._endpoint: SpeechEndpoint = speech_endpoints.selected_endpoint()
        self._token = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.server_combo = engine_combo(())
        self.model_combo = engine_combo(())
        self.server_field = engine_field(
            "Server", self.server_combo,
            "Choose where the API backend sends your audio: OpenAI, Groq, or a "
            "speech server you run, such as Speaches, LocalAI, vLLM or "
            "whisper.cpp. Audio sent to a server on this computer stays on it. "
            "Hosted services need an API key.",
            [("Open Settings → App → API keys", "api_keys")], self.help_requested.emit,
        )
        self.model_field = engine_field(
            "Model", self.model_combo,
            "The speech model the server turns your audio into text with. "
            "A server you run lists the models it has; choose Edit server… "
            "to enter another model id.",
            [("Open Settings → App → API keys", "api_keys")], self.help_requested.emit,
        )
        row.addWidget(self.server_field, stretch=1)
        row.addWidget(self.model_field, stretch=1)
        self.server_combo.currentIndexChanged.connect(self._on_server_changed)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        _catalog_relay().models_done.connect(self._on_models_done)
        self.refresh()

    @property
    def endpoint(self) -> SpeechEndpoint:
        return self._endpoint

    def refresh(self) -> None:
        """Show the saved server and model."""
        settings = settings_manager.load_all_settings()
        self._endpoint = endpoint = speech_endpoints.selected_endpoint(settings)
        combo = self.server_combo
        blocked = combo.blockSignals(True)
        combo.clear()
        for preset in ENDPOINTS:
            configured = speech_endpoints.configured_endpoint(preset.id, settings)
            name = preset.name if configured.is_configured else "Custom…"
            combo.addItem(name, preset.id)
            combo.setItemData(
                combo.count() - 1, preset.description, Qt.ItemDataRole.ToolTipRole,
            )
        combo.insertSeparator(combo.count())
        combo.addItem("Edit server…", _EDIT_OPTION)
        combo.model().item(combo.count() - 1).setEnabled(endpoint.configurable)
        combo.setCurrentIndex(combo.findData(endpoint.id))
        combo.blockSignals(blocked)
        combo.setToolTip(
            f"{endpoint.description} "
            f"{speech_endpoints.audio_destination_sentence(endpoint)}"
        )
        self._show_models(speech_endpoints.endpoint_model(endpoint, settings))

    def _show_models(self, current: str) -> None:
        endpoint = self._endpoint
        if endpoint.is_openai or endpoint.models_path is None:
            models = speech_endpoints.list_models(endpoint)
        else:
            models = _catalogs.get(_catalog_key(endpoint))
            if models is None:
                self._fetch_models()
                models = []
        if not endpoint.is_openai and current not in models:
            # The chosen id stays listed: the server may not report it (yet).
            models = [current, *models]
        combo = self.model_combo
        blocked = combo.blockSignals(True)
        combo.clear()
        for model in models:
            # Only OpenAI's ids carry its retirement dates.
            combo.addItem(api_model_label(model) if endpoint.is_openai else model, model)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.blockSignals(blocked)
        combo.setToolTip(
            f"{endpoint.name} model. {speech_endpoints.audio_destination_sentence(endpoint)}"
        )

    def _fetch_models(self) -> None:
        endpoint = self._endpoint
        self._token = token = next(_tokens)
        done = _catalog_relay().models_done
        list_models = speech_endpoints.list_models

        def worker():
            try:
                done.emit(token, endpoint, list_models(endpoint))
            except Exception as exc:
                logger.info("Couldn't list %s models: %s", endpoint.name, type(exc).__name__)
                done.emit(token, endpoint, None)

        threading.Thread(target=worker, name="speech-server-models", daemon=True).start()

    def _on_models_done(self, token: int, endpoint: SpeechEndpoint, models) -> None:
        if models is not None:
            _catalogs[_catalog_key(endpoint)] = models
        if token != self._token or _catalog_key(endpoint) != _catalog_key(self._endpoint):
            return
        if models is None:
            self.model_combo.setToolTip(
                f"Couldn't reach {endpoint.name} at {endpoint.base_url} to list its "
                "models. Check that the server is running, or choose Edit server…"
            )
            return
        self._show_models(self.model_combo.currentData() or "")

    def _restore_server(self) -> None:
        blocked = self.server_combo.blockSignals(True)
        self.server_combo.setCurrentIndex(self.server_combo.findData(self._endpoint.id))
        self.server_combo.blockSignals(blocked)

    def _on_server_changed(self, _index: int = 0) -> None:
        choice = self.server_combo.currentData()
        if choice is None:
            return
        if choice == _EDIT_OPTION:
            self._restore_server()
            self._edit(self._endpoint)
            return
        endpoint = speech_endpoints.configured_endpoint(choice)
        if choice == CUSTOM_ENDPOINT_ID and not endpoint.is_configured:
            self._restore_server()
            self._edit(endpoint, select=True)
            return
        speech_endpoints.save_selection(choice)
        # Picking a server is the moment to ask it what it has now.
        _catalogs.pop(_catalog_key(endpoint), None)
        self.refresh()
        self.changed.emit()

    def _edit(self, endpoint: SpeechEndpoint, select: bool = False) -> None:
        """Open the editor for ``endpoint``, selecting it too once saved if ``select``."""
        from ui_qt.dialogs.speech_server_dialog import SpeechServerDialog

        if not endpoint.configurable:
            return
        dialog = SpeechServerDialog(endpoint, self.window())
        if not dialog.exec():
            return
        if select:
            speech_endpoints.save_selection(endpoint.id)
        self.refresh()
        self.changed.emit()

    def _on_model_changed(self, _index: int = 0) -> None:
        model = self.model_combo.currentData()
        if not model:
            return
        speech_endpoints.save_model(self._endpoint.id, model)
        self.changed.emit()
