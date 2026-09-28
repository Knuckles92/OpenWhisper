"""A meeting-owned connection to a paired speech host.

The saved route contains no credentials. Every connection, including recovery,
must match its host identity and speech model before any audio is sent.
"""
from __future__ import annotations

import json

from transcriber.remote_backend import RemoteSpeechBackend


class RemoteMeetingUnavailable(RuntimeError):
    """Audio remains on disk while the meeting waits for its speech host."""


def remote_route(settings=None) -> dict:
    from services.remote_asr.settings import load_client_pairing

    pairing = load_client_pairing(settings)
    if pairing is None:
        raise ValueError("Pair a computer in Settings → Dictation → Remote engine first.")
    return {"fingerprint": pairing.fingerprint, "host_name": pairing.host_name}


def saved_remote_route(meeting: dict):
    raw = meeting.get("asr_remote_json")
    if raw is None:
        return None  # Older meetings always used local ASR.
    try:
        route = json.loads(raw)
        if not isinstance(route, dict) or not all(
            isinstance(route.get(key), str) and route[key]
            for key in ("fingerprint", "family", "model")
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("This meeting's remote speech configuration is invalid.") from exc
    if _is_this_computer(route["fingerprint"]):
        # A paired computer's meeting stored here, transcribed by this very
        # computer's engine: re-running it here is local speech.
        return None
    return route


def _is_this_computer(fingerprint: str) -> bool:
    import hmac

    from services.remote_asr.service import _default_identity_dir
    from services.remote_asr.tls import own_fingerprint

    try:
        own = own_fingerprint(_default_identity_dir())
    except Exception:
        return False
    return bool(own) and hmac.compare_digest(own, str(fingerprint).upper())


class MeetingRemoteBackend(RemoteSpeechBackend):
    """Independent of dictation; never switches the host's shared engine."""

    def __init__(self, route: dict):
        super().__init__()
        self.route = dict(route)
        self.host_name = str(route.get("host_name") or "the paired computer")

    def _check_pairing(self, pairing):
        if (pairing is None or not self.route.get("fingerprint")
                or pairing.fingerprint != self.route["fingerprint"]):
            raise RemoteMeetingUnavailable(
                f"Pair with {self.host_name} again to transcribe this meeting. "
                "Its audio will not be sent to a different computer."
            )

    def _connect(self, pairing, token, generation=None):
        self._check_pairing(pairing)
        connection, ready = super()._connect(pairing, token, generation)
        try:
            engine = ready.get("engine") or {}
            family, model = engine.get("family"), engine.get("model")
            from services.local_asr.catalog import MODELS
            from config import config

            supported = (
                family == "local_whisper" and model in config.WHISPER_MODEL_CHOICES
            ) or (
                model in MODELS and MODELS[model].backend == family and MODELS[model].meeting
            )
            if not engine.get("available"):
                raise RemoteMeetingUnavailable(
                    f"{self.host_name}: {engine.get('status') or 'speech engine is not ready'}."
                )
            if not supported:
                raise RemoteMeetingUnavailable(
                    f"{self.host_name}'s engine does not support meetings. Select Whisper, "
                    "Parakeet, Nemotron or Moonshine in Remote engine → Manage host models."
                )
            if self.route.get("model") and (
                family != self.route.get("family") or model != self.route["model"]
            ):
                raise RemoteMeetingUnavailable(
                    f"Restore {self.route['model']} on {self.host_name} to resume this meeting."
                )
            self.route.update(family=family, model=model)
            return connection, ready
        except Exception as exc:
            self.last_error = str(exc)
            connection.close()
            raise

    def ensure_ready(self):
        from services.remote_asr.settings import load_client_pairing

        if self.should_cancel:
            raise RemoteMeetingUnavailable("Meeting transcription stopped.")
        # Forgetting/replacing a pairing also revokes an already-open session.
        try:
            self._check_pairing(load_client_pairing())
        except RemoteMeetingUnavailable:
            self.cleanup()
            raise
        self.check_link()
        if not self.is_available():
            self.restore_link()
        if not self.is_available():
            raise RemoteMeetingUnavailable(self.last_error or f"Cannot connect to {self.host_name}.")

    def _request_audio(self, op, audio, language=None, **options):
        self.ensure_ready()
        try:
            return super()._request_audio(op, audio, language, **options)
        except RuntimeError as exc:
            if not self.is_available():
                raise RemoteMeetingUnavailable(str(exc)) from exc
            raise
