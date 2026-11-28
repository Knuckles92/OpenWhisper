"""Platform-agnostic data models for Meeting Mode call detection and auto-stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Mapping, Optional


class CallPolicy:
    """User policy for what OpenWhisper does when a call is detected."""

    ASK: Final[str] = "ask"
    ALWAYS_RECORD: Final[str] = "always_record"
    DONT_OFFER: Final[str] = "dont_offer"

    ALL: Final[tuple[str, ...]] = (ASK, ALWAYS_RECORD, DONT_OFFER)

    LABELS: Final[dict[str, str]] = {
        ASK: "Ask each time",
        ALWAYS_RECORD: "Record automatically",
        DONT_OFFER: "Don't offer",
    }

    @classmethod
    def normalize(cls, value: object, default: str = ASK) -> str:
        text = str(value or "").strip().lower()
        return text if text in cls.ALL else default


class CallSource(str, Enum):
    """Normalized call service or application category."""

    MANUAL = "manual"
    GOOGLE_MEET = "google_meet"
    ZOOM = "zoom"
    TEAMS = "teams"
    SLACK_HUDDLE = "slack_huddle"
    FACETIME = "facetime"
    WHATSAPP = "whatsapp"
    GATHER = "gather"
    DISCORD = "discord"
    BROWSER_CALL = "browser_call"
    SIGNAL = "signal"
    TELEGRAM = "telegram"
    WECHAT = "wechat"
    LINE = "line"
    WEBEX = "webex"
    ELEMENT = "element"
    GOOGLE_VOICE = "google_voice"
    MESSENGER = "messenger"
    GROUPME = "groupme"
    WHEREBY = "whereby"
    JITSI = "jitsi"

    @property
    def label(self) -> str:
        return _SOURCE_LABELS[self]


_SOURCE_LABELS: Final[dict[CallSource, str]] = {
    CallSource.MANUAL: "Meeting",
    CallSource.GOOGLE_MEET: "Google Meet",
    CallSource.ZOOM: "Zoom",
    CallSource.TEAMS: "Microsoft Teams",
    CallSource.SLACK_HUDDLE: "Slack huddle",
    CallSource.FACETIME: "FaceTime",
    CallSource.WHATSAPP: "WhatsApp",
    CallSource.GATHER: "Gather",
    CallSource.DISCORD: "Discord",
    CallSource.BROWSER_CALL: "Browser call",
    CallSource.SIGNAL: "Signal",
    CallSource.TELEGRAM: "Telegram",
    CallSource.WECHAT: "WeChat",
    CallSource.LINE: "LINE",
    CallSource.WEBEX: "Webex",
    CallSource.ELEMENT: "Element",
    CallSource.GOOGLE_VOICE: "Google Voice",
    CallSource.MESSENGER: "Messenger",
    CallSource.GROUPME: "GroupMe",
    CallSource.WHEREBY: "Whereby",
    CallSource.JITSI: "Jitsi Meet",
}


#: Known call services stabilize quickly (~2 s); generic browser mic usage and
#: messaging apps that send short voice notes require ~20 s.
KNOWN_CALL_STABLE_S: Final[float] = 2.0
GENERIC_CALL_STABLE_S: Final[float] = 20.0

#: Messaging apps / generic browser sources that use the long debounce so
#: voice notes or web dictation do not trigger call offers.
LONG_DEBOUNCE_SOURCES: Final[frozenset[CallSource]] = frozenset({
    CallSource.BROWSER_CALL,
    CallSource.WHATSAPP,
    CallSource.GATHER,
    CallSource.SIGNAL,
    CallSource.TELEGRAM,
    CallSource.WECHAT,
    CallSource.LINE,
    CallSource.ELEMENT,
    CallSource.MESSENGER,
    CallSource.GROUPME,
})


@dataclass(frozen=True)
class ActiveAudioProcess:
    """One OS process currently holding an audio input (microphone) stream."""

    pid: int
    bundle_id: str
    name: str


@dataclass(frozen=True)
class BrowserContext:
    """Optional browser tab/window metadata inspected when a browser uses the mic."""

    app_name: str = ""
    url: Optional[str] = None
    title: str = ""


@dataclass(frozen=True)
class CallProvider:
    """An app or website that shares one per-provider detection policy."""

    key: str
    label: str


@dataclass(frozen=True)
class CallCandidate:
    """A detected call candidate from microphone + optional browser context."""

    key: str
    title: str
    source: CallSource
    application: Optional[str] = None
    application_name: Optional[str] = None
    site: Optional[str] = None
    named: bool = False
    call_code: Optional[str] = None

    @property
    def required_stability_s(self) -> float:
        if self.source in LONG_DEBOUNCE_SOURCES:
            return GENERIC_CALL_STABLE_S
        return KNOWN_CALL_STABLE_S

    @property
    def is_unresolved_browser(self) -> bool:
        return self.source == CallSource.BROWSER_CALL and self.site is None

    def app_label(self) -> str:
        """Human-readable name of the call app or service."""
        if self.source == CallSource.MANUAL:
            return "Meeting"
        if self.source == CallSource.BROWSER_CALL:
            return self.site or self.application_name or "Browser"
        return self.source.label

    def offer_title(self) -> str:
        """Headline shown in the desktop offer toast."""
        if self.source == CallSource.SLACK_HUDDLE:
            return "Slack huddle detected"
        if self.source == CallSource.BROWSER_CALL:
            target = self.site or self.application_name
            return f"{target} call detected" if target else "Call detected"
        return f"{self.source.label} call detected"

    def offer_subtitle(self) -> str:
        """Secondary line shown in the desktop offer toast."""
        if self.named and self.title:
            return self.title
        return "Start Meeting Mode to record and transcribe?"

    def provider(self) -> Optional[CallProvider]:
        """The provider key and display label used for per-app/site policies."""
        from meeting.call_detection.catalog import (
            known_provider_label,
            provider_key_for_site,
        )

        site_key = provider_key_for_site(self.site) if self.site else None
        key = site_key or self.application
        if not key:
            return None
        label = (
            known_provider_label(key)
            or self.site
            or self.application_name
            or key
        )
        return CallProvider(key=key, label=label)

    def can_always_record(self) -> bool:
        """Whether this candidate may be set to 'Record automatically'.

        An unresolved browser call (unknown site) cannot be auto-recorded, as
        that would record every microphone use in the browser.
        """
        return self.provider() is not None and not self.is_unresolved_browser

    def resolve_policy(
        self,
        app_policies: Mapping[str, object],
        default_policy: str = CallPolicy.ASK,
    ) -> str:
        """Resolve the effective policy for this candidate.

        Precedence:
        1. Explicit site override (mapped to canonical provider key, e.g. Teams
           web shares Microsoft Teams' key; legacy non-Ask host overrides are
           still respected).
        2. Explicit application override (e.g. a browser's policy covers its
           other sites).
        3. Global default policy.
        """
        from meeting.call_detection.catalog import provider_key_for_site

        def _lookup(key: Optional[str]) -> Optional[str]:
            if not key or key not in app_policies:
                return None
            entry = app_policies[key]
            raw = entry.get("policy") if isinstance(entry, Mapping) else entry
            text = str(raw or "").strip().lower()
            if text in CallPolicy.ALL:
                return text
            return None

        raw_site = self.site
        mapped_site = provider_key_for_site(raw_site) if raw_site else None
        if raw_site and mapped_site != raw_site:
            legacy = _lookup(raw_site)
            if legacy is not None and legacy != CallPolicy.ASK:
                return legacy
        for key in (mapped_site, self.application):
            chosen = _lookup(key)
            if chosen is not None:
                return chosen
        return CallPolicy.normalize(default_policy)


@dataclass(frozen=True)
class DetectionOffer:
    """Ask the user whether to start Meeting Mode for a detected call."""

    candidate: CallCandidate
    offer_id: int


@dataclass(frozen=True)
class DetectionAutoRecord:
    """Start Meeting Mode automatically for a detected call."""

    candidate: CallCandidate


DetectionEvent = DetectionOffer | DetectionAutoRecord


@dataclass(frozen=True)
class CallEndGraceStarted:
    """The watched call app has been absent long enough to arm auto-stop."""

    app_label: str
    app_id: str
    deadline: float
    grace_s: float


@dataclass(frozen=True)
class CallEndGraceCancelled:
    """A pending auto-stop grace countdown was cancelled."""

    reason: str  # "resumed" | "user_kept" | "disabled"


@dataclass(frozen=True)
class CallEndStopDue:
    """The auto-stop grace countdown expired; stop Meeting Mode now."""

    app_label: str
    app_id: str


CallEndEvent = CallEndGraceStarted | CallEndGraceCancelled | CallEndStopDue


@dataclass(frozen=True)
class WatchedAppStatus:
    """Single observation of whether the watched call app is still present."""

    present: bool
    on_microphone: bool
    playing_audio: bool = False
    read_failed: bool = False
