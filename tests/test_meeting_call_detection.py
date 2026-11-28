"""Unit tests for Meeting Mode automatic call detection and auto-stop."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting.call_detection import (
    ABSENT_FOR_RESET_S,
    AUTO_STOP_GRACE_S,
    CALL_ENDED_AFTER_S,
    DROPOUT_GRACE_S,
    GENERIC_CALL_STABLE_S,
    KNOWN_CALL_STABLE_S,
    MAX_OBSERVATION_GAP_S,
    ActiveAudioProcess,
    BrowserContext,
    CallCandidate,
    CallDetectionWatcher,
    CallDetector,
    CallEndGraceCancelled,
    CallEndGraceStarted,
    CallEndStopDue,
    CallEndTracker,
    CallPolicy,
    CallSource,
    DetectionAutoRecord,
    DetectionOffer,
    WatchedAppStatus,
    canonical_app_id,
    candidate_from_microphone,
    is_browser_id,
    only_native_call_app,
    pick_primary_process,
    provider_key_for_site,
)
from meeting.call_detection.adapters import (
    AudioActivityAdapter,
    NullAudioActivityAdapter,
    create_platform_adapter,
)
from meeting.call_detection.adapters._linux import (
    IncrementalJsonParser,
    LinuxPipeWireAdapter,
    PipeWireGraph,
)


# ---------------------------------------------------------------------------
# Catalog & Cross-Platform Normalization
# ---------------------------------------------------------------------------


class TestCatalogNormalization:
    @pytest.mark.parametrize(
        "bundle_id,name,expected",
        [
            ("us.zoom.xos", "zoom.us", "us.zoom.xos"),
            ("zoom.exe", "Zoom", "us.zoom.xos"),
            ("zoom", "zoom", "us.zoom.xos"),
            ("com.microsoft.teams2", "Microsoft Teams", "com.microsoft.teams2"),
            ("com.microsoft.teams", "Microsoft Teams", "com.microsoft.teams2"),
            ("ms-teams.exe", "ms-teams", "com.microsoft.teams2"),
            ("teams-for-linux", "teams-for-linux", "com.microsoft.teams2"),
            ("com.tinyspeck.slackmacgap.helper", "Slack Helper", "com.tinyspeck.slackmacgap"),
            ("slack.exe", "Slack", "com.tinyspeck.slackmacgap"),
            ("com.hnc.Discord.helper", "Discord Helper", "com.hnc.Discord"),
            ("vesktop", "Vesktop", "com.hnc.Discord"),
            ("com.apple.FaceTime", "FaceTime", "com.apple.FaceTime"),
            ("net.whatsapp.WhatsApp", "WhatsApp", "net.whatsapp.WhatsApp"),
            ("webex.exe", "Webex", "com.cisco.webexmeetingsapp"),
            ("com.google.Chrome.helper", "Google Chrome Helper", "com.google.Chrome"),
            ("chrome.exe", "chrome", "com.google.Chrome"),
            ("google-chrome", "Google Chrome", "com.google.Chrome"),
            ("com.apple.WebKit.GPU", "Safari Graphics and Media", "com.apple.Safari"),
            ("msedge.exe", "msedge", "com.microsoft.edgemac"),
            ("brave-browser", "Brave", "com.brave.Browser"),
            ("firefox", "Firefox", "org.mozilla.firefox"),
            ("company.thebrowser.browser.helper", "Browser Helper", "company.thebrowser.Browser"),
            ("vivaldi.exe", "vivaldi", "com.vivaldi.Vivaldi"),
        ],
    )
    def test_canonical_app_id(self, bundle_id, name, expected):
        assert canonical_app_id(bundle_id, name) == expected

    def test_unknown_app_returns_none(self):
        assert canonical_app_id("com.apple.VoiceMemos", "Voice Memos") is None
        assert canonical_app_id("audacity", "Audacity") is None
        assert canonical_app_id("com.apple.WebKit.GPU", "Mail") is None

    def test_pick_primary_process_prefers_native_call_app_over_browser(self):
        procs = [
            ActiveAudioProcess(pid=10, bundle_id="chrome.exe", name="chrome"),
            ActiveAudioProcess(pid=20, bundle_id="zoom.exe", name="zoom"),
        ]
        primary = pick_primary_process(procs)
        assert primary is not None
        assert canonical_app_id(primary.bundle_id, primary.name) == "us.zoom.xos"

    def test_only_native_call_app(self):
        assert only_native_call_app([]) is None
        assert only_native_call_app(
            [ActiveAudioProcess(pid=1, bundle_id="zoom", name="zoom")]
        ) == ("us.zoom.xos", "Zoom")
        # Helper + main process fold to one native call app
        assert only_native_call_app(
            [
                ActiveAudioProcess(pid=1, bundle_id="com.tinyspeck.slackmacgap.helper", name="Slack Helper"),
                ActiveAudioProcess(pid=2, bundle_id="com.tinyspeck.slackmacgap", name="Slack"),
            ]
        ) == ("com.tinyspeck.slackmacgap", "Slack huddle")
        # Two distinct native call apps -> ambiguous -> None
        assert only_native_call_app(
            [
                ActiveAudioProcess(pid=1, bundle_id="zoom", name="zoom"),
                ActiveAudioProcess(pid=2, bundle_id="ms-teams.exe", name="ms-teams"),
            ]
        ) is None
        # Browser alone -> None
        assert only_native_call_app(
            [ActiveAudioProcess(pid=1, bundle_id="chrome.exe", name="chrome")]
        ) is None


class TestBrowserContextMatching:
    def test_google_meet_by_url_and_title(self):
        arc = ActiveAudioProcess(
            pid=5120, bundle_id="company.thebrowser.browser.helper", name="Browser Helper"
        )
        cand = candidate_from_microphone(
            arc,
            BrowserContext(
                app_name="Arc",
                url="https://meet.google.com/abc-defg-hij",
                title="Meet - Design sync - Arc",
            ),
        )
        assert cand is not None
        assert cand.source == CallSource.GOOGLE_MEET
        assert cand.application == "company.thebrowser.Browser"
        assert cand.site == "meet.google.com"
        assert cand.named is True
        assert cand.title == "Design sync"
        assert cand.call_code == "abc-defg-hij"
        assert cand.required_stability_s == KNOWN_CALL_STABLE_S
        assert cand.can_always_record() is True

    def test_google_meet_by_title_without_url(self):
        chrome = ActiveAudioProcess(pid=100, bundle_id="chrome.exe", name="chrome")
        cand = candidate_from_microphone(
            chrome, BrowserContext(app_name="Google Chrome", url=None, title="Meet - Sprint Review - Google Chrome")
        )
        assert cand is not None
        assert cand.source == CallSource.GOOGLE_MEET
        assert cand.title == "Sprint Review"
        assert cand.named is True
        assert cand.site == "meet.google.com"
        assert cand.required_stability_s == KNOWN_CALL_STABLE_S

        # Landing page stays generic browser call (20 s debounce)
        landing = candidate_from_microphone(
            chrome, BrowserContext(app_name="Google Chrome", url=None, title="Google Meet - Google Chrome")
        )
        assert landing is not None
        assert landing.source == CallSource.BROWSER_CALL
        assert landing.site is None
        assert landing.required_stability_s == GENERIC_CALL_STABLE_S
        assert landing.can_always_record() is False

    def test_teams_web_shares_teams_app_provider_key(self):
        edge = ActiveAudioProcess(pid=200, bundle_id="msedge.exe", name="msedge")
        cand = candidate_from_microphone(
            edge,
            BrowserContext(
                app_name="Microsoft Edge",
                url="https://teams.microsoft.com/v2/",
                title="Microsoft Teams",
            ),
        )
        assert cand is not None
        assert cand.source == CallSource.TEAMS
        assert provider_key_for_site("teams.microsoft.com") == "com.microsoft.teams2"
        provider = cand.provider()
        assert provider is not None
        assert provider.key == "com.microsoft.teams2"
        assert provider.label == "Microsoft Teams"
        # Teams desktop policy covers Teams web!
        assert cand.resolve_policy({"com.microsoft.teams2": "always_record"}, CallPolicy.ASK) == CallPolicy.ALWAYS_RECORD

    def test_zoom_web_and_generic_messaging_debounces(self):
        chrome = ActiveAudioProcess(pid=1, bundle_id="com.google.Chrome", name="Chrome")
        zoom_web = candidate_from_microphone(
            chrome, BrowserContext(url="https://us02web.zoom.us/wc/join/123", title="Zoom Meeting")
        )
        assert zoom_web is not None
        assert zoom_web.source == CallSource.ZOOM
        assert zoom_web.required_stability_s == KNOWN_CALL_STABLE_S

        whatsapp = candidate_from_microphone(
            ActiveAudioProcess(pid=2, bundle_id="net.whatsapp.WhatsApp", name="WhatsApp")
        )
        assert whatsapp is not None
        assert whatsapp.required_stability_s == GENERIC_CALL_STABLE_S


# ---------------------------------------------------------------------------
# CallDetector State Machine (Debounce, Policies, Snooze)
# ---------------------------------------------------------------------------


class TestCallDetector:
    @staticmethod
    def _zoom() -> CallCandidate:
        cand = candidate_from_microphone(
            ActiveAudioProcess(pid=10, bundle_id="us.zoom.xos", name="zoom.us")
        )
        assert cand is not None
        return cand

    @staticmethod
    def _generic_chrome() -> CallCandidate:
        cand = candidate_from_microphone(
            ActiveAudioProcess(pid=20, bundle_id="com.google.Chrome", name="Chrome")
        )
        assert cand is not None
        return cand

    def test_known_call_debounces_2s_and_offers(self):
        det = CallDetector()
        zoom = self._zoom()
        assert det.observe(zoom, {}, CallPolicy.ASK, now=10.0) is None
        assert det.observe(zoom, {}, CallPolicy.ASK, now=11.5) is None
        ev = det.observe(zoom, {}, CallPolicy.ASK, now=12.0)
        assert isinstance(ev, DetectionOffer)
        assert ev.candidate == zoom
        assert det.open_offer_id == ev.offer_id

        # Snoozed while still present
        assert det.observe(zoom, {}, CallPolicy.ASK, now=15.0) is None

    def test_generic_browser_requires_20s_debounce_and_cannot_auto_record(self):
        det = CallDetector()
        chrome = self._generic_chrome()
        assert det.observe(chrome, {}, CallPolicy.ALWAYS_RECORD, now=0.0) is None
        assert det.observe(chrome, {}, CallPolicy.ALWAYS_RECORD, now=10.0) is None
        assert det.observe(chrome, {}, CallPolicy.ALWAYS_RECORD, now=19.5) is None
        ev = det.observe(chrome, {}, CallPolicy.ALWAYS_RECORD, now=20.0)
        # Unresolved browser cannot auto-record; falls back to Offer
        assert isinstance(ev, DetectionOffer)

    def test_dropout_grace_preserves_pending_stability(self):
        det = CallDetector()
        zoom = self._zoom()
        assert det.observe(zoom, {}, CallPolicy.ASK, now=0.0) is None
        assert det.observe(zoom, {}, CallPolicy.ASK, now=1.0) is None
        # Brief 1.5 s dropout (<= DROPOUT_GRACE_S)
        assert det.observe(None, {}, CallPolicy.ASK, now=2.0) is None
        ev = det.observe(zoom, {}, CallPolicy.ASK, now=2.5)
        assert isinstance(ev, DetectionOffer)

    def test_large_observation_gap_restarts_stability(self):
        det = CallDetector()
        zoom = self._zoom()
        assert det.observe(zoom, {}, CallPolicy.ASK, now=0.0) is None
        # 15 s sleep gap (> MAX_OBSERVATION_GAP_S) must NOT immediately fire
        assert det.observe(zoom, {}, CallPolicy.ASK, now=15.0) is None
        ev = det.observe(zoom, {}, CallPolicy.ASK, now=17.0)
        assert isinstance(ev, DetectionOffer)

    def test_always_record_and_dont_offer_policies(self):
        det = CallDetector()
        zoom = self._zoom()
        assert det.observe(zoom, {"us.zoom.xos": "dont_offer"}, CallPolicy.ASK, now=0.0) is None
        assert det.observe(zoom, {"us.zoom.xos": "dont_offer"}, CallPolicy.ASK, now=5.0) is None

        # Always record fires DetectionAutoRecord after 2 s
        assert det.observe(zoom, {"us.zoom.xos": "always_record"}, CallPolicy.ASK, now=10.0) is None
        ev = det.observe(zoom, {"us.zoom.xos": "always_record"}, CallPolicy.ASK, now=12.0)
        assert isinstance(ev, DetectionAutoRecord)
        assert ev.candidate == zoom

    def test_offer_timeout_and_stale_timeout_safety(self):
        det = CallDetector()
        zoom = self._zoom()
        det.observe(zoom, {}, CallPolicy.ASK, now=0.0)
        ev = det.observe(zoom, {}, CallPolicy.ASK, now=2.0)
        assert isinstance(ev, DetectionOffer)

        # Recording stopped withdraws the open offer and sets Stopped snooze (3 s grace)
        det.recording_stopped(now=2.5)
        assert det.open_offer_id is None
        assert det.take_withdrawn_offers() == [ev.offer_id]

        # Late timeout for that offer must be ignored (cannot overwrite Stopped snooze)
        det.offer_timed_out(ev.offer_id)
        # Still snoozed for < 3 s of absence
        assert det.observe(None, {}, CallPolicy.ASK, now=4.0) is None
        assert det.observe(zoom, {}, CallPolicy.ASK, now=4.5) is None
        # Absent for >= 3 s clears Stopped snooze -> next call is offered
        assert det.observe(None, {}, CallPolicy.ASK, now=8.0) is None
        assert det.observe(zoom, {}, CallPolicy.ASK, now=8.5) is None
        ev2 = det.observe(zoom, {}, CallPolicy.ASK, now=10.5)
        assert isinstance(ev2, DetectionOffer)

    def test_timed_out_offer_resets_immediately_on_absence(self):
        det = CallDetector()
        zoom = self._zoom()
        det.observe(zoom, {}, CallPolicy.ASK, now=0.0)
        ev = det.observe(zoom, {}, CallPolicy.ASK, now=2.0)
        assert isinstance(ev, DetectionOffer)

        det.offer_timed_out(ev.offer_id)
        # Still on mic -> remains snoozed
        assert det.observe(zoom, {}, CallPolicy.ASK, now=5.0) is None
        # Leaves mic -> resets immediately
        assert det.observe(None, {}, CallPolicy.ASK, now=5.5) is None
        assert det.observe(zoom, {}, CallPolicy.ASK, now=6.0) is None
        ev2 = det.observe(zoom, {}, CallPolicy.ASK, now=8.0)
        assert isinstance(ev2, DetectionOffer)

    def test_switching_google_meet_rooms_not_snoozed(self):
        det = CallDetector()
        chrome = ActiveAudioProcess(pid=1, bundle_id="com.google.Chrome", name="Chrome")
        room1 = candidate_from_microphone(
            chrome, BrowserContext(url="https://meet.google.com/aaa-bbbb-ccc", title="Meet - Room 1")
        )
        room2 = candidate_from_microphone(
            chrome, BrowserContext(url="https://meet.google.com/xxx-yyyy-zzz", title="Meet - Room 2")
        )
        assert room1 is not None and room2 is not None

        det.observe(room1, {}, CallPolicy.ASK, now=0.0)
        ev1 = det.observe(room1, {}, CallPolicy.ASK, now=2.0)
        assert isinstance(ev1, DetectionOffer)
        det.mark_offer_answered(ev1.offer_id)

        # Switching to room2 is a distinct call_code and gets offered!
        assert det.observe(room2, {}, CallPolicy.ASK, now=3.0) is None
        ev2 = det.observe(room2, {}, CallPolicy.ASK, now=5.0)
        assert isinstance(ev2, DetectionOffer)
        assert ev2.candidate.call_code == "xxx-yyyy-zzz"


# ---------------------------------------------------------------------------
# CallEndTracker (Auto-Stop & Mute Safety)
# ---------------------------------------------------------------------------


class TestCallEndTracker:
    def test_mute_keeps_call_present_via_audio_playback(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=True)
        # User mutes: off mic, but Zoom still has audio playback open
        for t in range(0, 40):
            ev = tracker.observe(
                WatchedAppStatus(present=True, on_microphone=False, playing_audio=True),
                auto_stop_enabled=True,
                now=float(t),
            )
            assert ev is None
            assert tracker.armed is False

    def test_absence_3s_arms_30s_grace_and_stops_when_due(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=True)
        absent = WatchedAppStatus(present=False, on_microphone=False, playing_audio=False)

        assert tracker.observe(absent, auto_stop_enabled=True, now=10.0) is None
        assert tracker.observe(absent, auto_stop_enabled=True, now=12.5) is None
        ev = tracker.observe(absent, auto_stop_enabled=True, now=13.0)
        assert isinstance(ev, CallEndGraceStarted)
        assert ev.app_label == "Zoom"
        assert ev.grace_s == AUTO_STOP_GRACE_S
        assert ev.deadline == pytest.approx(13.0 + AUTO_STOP_GRACE_S)

        assert tracker.observe(absent, auto_stop_enabled=True, now=42.0) is None
        stop_ev = tracker.observe(absent, auto_stop_enabled=True, now=43.0)
        assert isinstance(stop_ev, CallEndStopDue)
        assert stop_ev.app_id == "us.zoom.xos"

    def test_resuming_during_grace_cancels_stop(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=True)
        absent = WatchedAppStatus(present=False, on_microphone=False, playing_audio=False)

        tracker.observe(absent, auto_stop_enabled=True, now=0.0)
        ev = tracker.observe(absent, auto_stop_enabled=True, now=3.0)
        assert isinstance(ev, CallEndGraceStarted)

        # App resumes playback or mic during the 30 s grace window
        cancel_ev = tracker.observe(
            WatchedAppStatus(present=True, on_microphone=False, playing_audio=True),
            auto_stop_enabled=True,
            now=15.0,
        )
        assert isinstance(cancel_ev, CallEndGraceCancelled)
        assert cancel_ev.reason == "resumed"
        assert tracker.armed is False

    def test_keep_recording_suppresses_auto_stop_for_rest_of_session(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=True)
        absent = WatchedAppStatus(present=False, on_microphone=False, playing_audio=False)

        tracker.observe(absent, auto_stop_enabled=True, now=0.0)
        assert isinstance(tracker.observe(absent, auto_stop_enabled=True, now=3.0), CallEndGraceStarted)

        cancel_ev = tracker.keep_recording()
        assert isinstance(cancel_ev, CallEndGraceCancelled)
        assert cancel_ev.reason == "user_kept"
        assert tracker.user_kept is True

        # Subsequent absence never re-arms
        for t in (10.0, 20.0, 60.0):
            assert tracker.observe(absent, auto_stop_enabled=True, now=t) is None

    def test_manual_recording_waits_until_seen_on_microphone(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=False)
        absent = WatchedAppStatus(present=False, on_microphone=False, playing_audio=False)

        # Never seen on mic yet -> absence does not arm
        for t in (0.0, 5.0, 10.0):
            assert tracker.observe(absent, auto_stop_enabled=True, now=t) is None

        # Seen on mic -> subsequent absence arms after 3 s
        assert tracker.observe(
            WatchedAppStatus(present=True, on_microphone=True), auto_stop_enabled=True, now=11.0
        ) is None
        assert tracker.observe(absent, auto_stop_enabled=True, now=12.0) is None
        assert isinstance(tracker.observe(absent, auto_stop_enabled=True, now=15.0), CallEndGraceStarted)

    def test_auto_stop_disabled_never_arms_and_cancels_if_toggled_off(self):
        tracker = CallEndTracker("us.zoom.xos", "Zoom", started_automatically=True)
        absent = WatchedAppStatus(present=False, on_microphone=False, playing_audio=False)

        assert tracker.observe(absent, auto_stop_enabled=False, now=0.0) is None
        assert tracker.observe(absent, auto_stop_enabled=False, now=5.0) is None

        tracker.observe(absent, auto_stop_enabled=True, now=10.0)
        assert isinstance(tracker.observe(absent, auto_stop_enabled=True, now=13.0), CallEndGraceStarted)
        cancel_ev = tracker.observe(absent, auto_stop_enabled=False, now=15.0)
        assert isinstance(cancel_ev, CallEndGraceCancelled)
        assert cancel_ev.reason == "disabled"


# ---------------------------------------------------------------------------
# Linux PipeWire Graph & Incremental JSON Parser
# ---------------------------------------------------------------------------


class TestLinuxPipeWireGraph:
    def test_graph_tracks_input_output_excludes_monitors_and_own_pid(self):
        graph = PipeWireGraph()
        initial_dump = [
            {
                "id": 10,
                "type": "PipeWire:Interface:Client",
                "info": {
                    "props": {
                        "application.process.id": 4242,
                        "application.process.binary": "zoom",
                        "application.name": "ZOOM VoiceEngine",
                    }
                },
            },
            {
                "id": 20,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "client.id": 10,
                    },
                },
            },
            # Sink monitor stream (e.g. OpenWhisper system audio capture) must be ignored!
            {
                "id": 21,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "stream.capture.sink": True,
                        "application.process.id": 9999,
                        "application.process.binary": "obs",
                    },
                },
            },
            # Own PID must be ignored
            {
                "id": 22,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Input/Audio",
                        "application.process.id": 1234,
                        "application.process.binary": "python3",
                    },
                },
            },
            # Output playback node for Zoom
            {
                "id": 30,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "client.id": 10,
                    },
                },
            },
        ]
        graph.apply_payload(initial_dump)
        assert graph.ready is True

        mics = graph.microphone_processes(own_pid=1234)
        assert len(mics) == 1
        assert mics[0].pid == 4242
        assert mics[0].bundle_id == "zoom"
        assert graph.is_app_playing("us.zoom.xos", own_pid=1234) is True

        # Remove mic node (muted) -> off mic, still playing audio!
        graph.apply_object({"id": 20, "info": None})
        assert graph.microphone_processes(own_pid=1234) == []
        assert graph.is_app_playing("us.zoom.xos", own_pid=1234) is True

        # Remove output node (hung up) -> no longer playing
        graph.apply_object({"id": 30, "info": None})
        assert graph.is_app_playing("us.zoom.xos", own_pid=1234) is False

    def test_incremental_json_parser_handles_chunked_arrays(self):
        parser = IncrementalJsonParser()
        doc1 = json.dumps([{"id": 1, "type": "PipeWire:Interface:Node"}])
        doc2 = json.dumps([{"id": 2, "info": None}])
        combined = doc1 + "\n" + doc2 + "\n"

        parsed = []
        for i in range(0, len(combined), 7):
            parsed.extend(parser.feed(combined[i : i + 7]))
        assert len(parsed) == 2
        assert parsed[0][0]["id"] == 1
        assert parsed[1][0]["id"] == 2

    def test_platform_adapter_factory_lazy_fallback(self):
        assert isinstance(create_platform_adapter("freebsd"), NullAudioActivityAdapter)
        assert isinstance(create_platform_adapter("linux"), LinuxPipeWireAdapter)


# ---------------------------------------------------------------------------
# CallDetectionWatcher End-to-End Orchestration
# ---------------------------------------------------------------------------


class _FakeAdapter(AudioActivityAdapter):
    def __init__(self) -> None:
        self.mic_procs: list[ActiveAudioProcess] = []
        self.playing: set[str] = set()
        self.contexts: dict[str, BrowserContext] = {}
        self.mic_calls: int = 0

    def active_microphone_processes(self) -> list[ActiveAudioProcess]:
        self.mic_calls += 1
        return list(self.mic_procs)

    def is_app_playing_audio(self, canonical_id: str) -> bool:
        return canonical_id in self.playing

    def browser_context(self, canonical_browser_id: str):
        return self.contexts.get(canonical_browser_id)


class TestCallDetectionWatcher:
    def test_skips_os_polling_when_disabled_with_no_overrides(self):
        adapter = _FakeAdapter()
        watcher = CallDetectionWatcher(
            adapter=adapter,
            get_default_policy=lambda: CallPolicy.DONT_OFFER,
            get_app_policies=lambda: {},
        )
        watcher.step(now=1.0)
        watcher.step(now=2.0)
        assert adapter.mic_calls == 0

    def test_end_to_end_offer_seen_provider_and_auto_stop(self):
        adapter = _FakeAdapter()
        offers = []
        seen = []
        grace_started = []
        grace_cancelled = []
        stop_due = []
        busy = {"active": False}

        watcher = CallDetectionWatcher(
            adapter=adapter,
            get_default_policy=lambda: CallPolicy.ASK,
            get_app_policies=lambda: {},
            get_auto_stop_enabled=lambda: True,
            is_meeting_busy=lambda: busy["active"],
            on_offer=lambda cand, oid: offers.append((cand, oid)),
            on_provider_seen=lambda k, lbl: seen.append((k, lbl)),
            on_call_end_grace_started=lambda lbl, g: grace_started.append((lbl, g)),
            on_call_end_grace_cancelled=lambda r: grace_cancelled.append(r),
            on_call_end_stop_due=lambda lbl: stop_due.append(lbl),
        )

        # Zoom takes mic -> seen immediately, offered after 2 s
        adapter.mic_procs = [ActiveAudioProcess(pid=1, bundle_id="zoom", name="zoom")]
        watcher.step(now=0.0)
        watcher.step(now=2.0)
        assert seen == [("us.zoom.xos", "Zoom")]
        assert len(offers) == 1
        cand, oid = offers[0]
        assert cand.source == CallSource.ZOOM

        # User accepts offer -> meeting becomes active
        watcher.mark_offer_answered(oid)
        busy["active"] = True
        watcher.notify_meeting_started(
            app_id=cand.application, app_label=cand.app_label(), started_automatically=False
        )
        # First pass during meeting sees Zoom on mic
        watcher.step(now=3.0)

        # User mutes (off mic, still playing audio) -> no grace countdown!
        adapter.mic_procs = []
        adapter.playing = {"us.zoom.xos"}
        watcher.step(now=5.0)
        watcher.step(now=10.0)
        assert grace_started == []

        # User hangs up (off mic and off playback) -> 3 s later grace starts
        adapter.playing = set()
        watcher.step(now=11.0)
        watcher.step(now=14.0)
        assert grace_started == [("Zoom", 30)]

        # 30 s later -> stop due!
        watcher.step(now=44.0)
        assert stop_due == ["Zoom"]


# ---------------------------------------------------------------------------
# UI Toast & Settings Integration Tests (Offscreen Qt)
# ---------------------------------------------------------------------------


class TestCallDetectionUIAndSettings:
    @pytest.fixture(autouse=True)
    def _qt_app(self):
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def test_call_detection_card_offer_and_call_ended_modes(self):
        from ui_qt.widgets.call_detection_toast import CallDetectionCard

        card = CallDetectionCard()
        try:
            zoom = candidate_from_microphone(
                ActiveAudioProcess(pid=1, bundle_id="us.zoom.xos", name="zoom.us")
            )
            assert zoom is not None

            started = []
            not_now = []
            card.start_requested.connect(lambda c, oid: started.append((c, oid)))
            card.not_now_requested.connect(lambda oid: not_now.append(oid))

            card.present_offer(zoom, offer_id=42)
            assert card.mode == "offer"
            assert card.title_label.text() == "Zoom call detected"
            assert card.always_record_action.isVisible() is True

            card.start_btn.click()
            assert started == [(zoom, 42)]
            assert card.mode == "idle"

            # Unresolved browser hides "Always record"
            chrome = candidate_from_microphone(
                ActiveAudioProcess(pid=2, bundle_id="com.google.Chrome", name="Chrome")
            )
            assert chrome is not None
            card.present_offer(chrome, offer_id=43)
            assert card.always_record_action.isVisible() is False
            card.not_now_btn.click()
            assert not_now == [43]

            # Call-ended countdown mode
            kept = []
            stopped = []
            card.keep_recording_requested.connect(lambda: kept.append(True))
            card.stop_now_requested.connect(lambda: stopped.append(True))

            card.present_call_ended("Zoom", grace_s=30)
            assert card.mode == "call_ended"
            assert card.title_label.text() == "Zoom call ended"
            assert card.subtitle_label.text() == "Stopping in 30 s"

            card.keep_recording_btn.click()
            assert kept == [True]
            assert card.mode == "idle"

            card.present_call_ended("Zoom", grace_s=30)
            card.stop_now_btn.click()
            assert stopped == [True]
            assert card.mode == "idle"
        finally:
            card.close()

    def test_settings_meeting_calls_page_persists_and_reloads(self):
        from services.settings import (
            MeetingCallDetectionPolicy,
            SettingsKey,
            SettingsManager,
        )
        from ui_qt.dialogs import settings_dialog as module
        from ui_qt.dialogs.settings_destinations import MEETING_CALLS

        with tempfile.TemporaryDirectory() as folder:
            manager = SettingsManager(os.path.join(folder, "settings.json"))
            with patch.object(module, "settings_manager", manager):
                dialog = module.SettingsDialog()
                try:
                    dialog.ensure_page(MEETING_CALLS)
                    combo = dialog.meeting_call_policy_combo
                    check = dialog.meeting_call_auto_stop_check

                    assert combo.currentData() == MeetingCallDetectionPolicy.ASK
                    assert check.isChecked() is False

                    # Change default policy & enable auto-stop
                    combo.setCurrentIndex(combo.findData(MeetingCallDetectionPolicy.ALWAYS_RECORD))
                    check.setChecked(True)
                    assert manager.get(SettingsKey.MEETING_CALL_DETECTION_POLICY) == MeetingCallDetectionPolicy.ALWAYS_RECORD
                    assert manager.get(SettingsKey.MEETING_CALL_AUTO_STOP) is True

                    # Change per-app policy for Zoom
                    zoom_combo = dialog.meeting_calls._app_combos["us.zoom.xos"]
                    zoom_combo.setCurrentIndex(zoom_combo.findData(MeetingCallDetectionPolicy.DONT_OFFER))
                    saved_apps = manager.get(SettingsKey.MEETING_CALL_DETECTION_APPS)
                    assert saved_apps["us.zoom.xos"]["policy"] == MeetingCallDetectionPolicy.DONT_OFFER
                finally:
                    dialog.close()

                # Reload in a fresh SettingsDialog and verify persistence
                dialog2 = module.SettingsDialog()
                try:
                    dialog2.ensure_page(MEETING_CALLS)
                    assert dialog2.meeting_call_policy_combo.currentData() == MeetingCallDetectionPolicy.ALWAYS_RECORD
                    assert dialog2.meeting_call_auto_stop_check.isChecked() is True
                    assert (
                        dialog2.meeting_calls._app_combos["us.zoom.xos"].currentData()
                        == MeetingCallDetectionPolicy.DONT_OFFER
                    )
                finally:
                    dialog2.close()
