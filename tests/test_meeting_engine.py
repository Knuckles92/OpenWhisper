"""
Tests for MeetingEngine orchestration: intelligence health reporting, the
scheduler health signal, consent-revocation teardown, end-failure unwinding,
and mid-meeting diarizer degradation.

Every subsystem the engine imports lazily (capture, ASR, diarizer, web server,
agent core, scheduler) is replaced with an in-process fake, so nothing here
touches an audio device, a Whisper model, a socket, or the network.
"""
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

from meeting.interfaces import AgentResult, SpooledChunk, TranscriptSegment

# Fakes

class FakeSource:
    """Capture source that reports itself active without opening a device."""

    def __init__(self, channel, index=0, samplerate=16000, channels=1):
        self.channel = channel
        self.device_id = index
        self.started = False
        self.stopped = False
        self.callback = None

    def start(self, callback):
        self.callback = callback
        self.started = True

    def is_active(self):
        return self.started and not self.stopped

    def stop(self):
        self.stopped = True

class FakeSpool:
    """Spool writer that swallows blocks and never produces chunks."""

    def __init__(self, meeting_id, channel, spool_dir, clock, repository,
                 on_chunk=None, initial_seq=0):
        self.channel = channel
        self.on_chunk = on_chunk
        self.flushes = 0

    def feed(self, block):
        pass

    def flush(self):
        self.flushes += 1
        return None

class FakeAsr:
    """ASR engine stand-in; records lifecycle calls."""

    instances = []

    def __init__(self, model, meeting_id, repository, language=None,
                 enable_revisions=True):
        self.model = model
        self.meeting_id = meeting_id
        self.language = language
        self.enable_revisions = enable_revisions
        self.is_available = True
        self.on_segments = None
        self.chunks = []
        self.drains = 0
        self.stops = 0
        self.requeues = 0
        self.offline_passes = 0
        self.offline_segments = []
        FakeAsr.instances.append(self)

    def start(self, on_segments):
        self.on_segments = on_segments

    def enqueue(self, chunk):
        self.chunks.append(chunk)

    def drain(self, timeout_s):
        self.drains += 1
        return True

    def transcribe_offline_session(self, spool_dir, chunks=None):
        self.offline_passes += 1
        return list(self.offline_segments)

    def stop(self):
        self.stops += 1

    def requeue_pending(self):
        self.requeues += 1

class FakeServer:
    """Web server stand-in that records broadcasts instead of sending them."""

    instances = []

    def __init__(self, engine, repository, bind="localhost", port=0):
        self.engine = engine
        self.host_url = "http://127.0.0.1:9999/h/token"
        self.guest_url = "http://127.0.0.1:9999/g/token"
        self.messages = []
        self.stopped = False
        FakeServer.instances.append(self)

    def start(self):
        return self.host_url

    def broadcast(self, message):
        self.messages.append(dict(message))

    def stop(self):
        self.stopped = True

class FakeDiarizer:
    """Diarizer whose availability and assignments the test drives."""

    def __init__(self):
        self.available = True
        self.next_participant = None
        self.assigned = []
        self.pins = []
        self.relabel_cb = None
        self.availability_probes = 0

    def is_available(self):
        self.availability_probes += 1
        return self.available

    def assign(self, segment, audio, sample_rate):
        self.assigned.append(segment.segment_id)
        return self.next_participant

    def set_relabel_callback(self, cb):
        self.relabel_cb = cb

    def pin(self, segment_id, participant_id):
        self.pins.append((segment_id, participant_id))

class FakeAgentCore:
    """Agent core stand-in with a settable health verdict."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.config = None
        self.cancels = 0
        self.shutdowns = 0

    def initialize(self, cfg, tools):
        self.config = cfg

    def checkpoint(self, payload):
        return AgentResult(ok=True)

    def consolidate(self, payload):
        return AgentResult(ok=True)

    def cancel(self):
        self.cancels += 1

    def is_healthy(self):
        return self.healthy

    def shutdown(self):
        self.shutdowns += 1

class FakeScheduler:
    """Checkpoint scheduler stand-in recording its construction kwargs."""

    instances = []

    def __init__(self, engine, agent_core, base_interval_s=15.0,
                 min_interval_s=5.0, max_interval_s=20.0, on_health=None):
        self.engine = engine
        self.agent_core = agent_core
        self.on_health = on_health
        self.started = False
        self.stopped = False
        self.consolidations = 0
        self.polishes = 0
        self.seeded = None
        FakeScheduler.instances.append(self)

    def _mark_sent(self, segments):
        self.seeded = list(segments)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def prepare_for_end(self):
        return None

    def notify_segments(self, count):
        pass

    def run_final_polish(self, timeout_s=60.0):
        from meeting.agent.scheduler import ConsolidationOutcome

        self.polishes += 1
        return ConsolidationOutcome(
            status="completed",
            message="Transcript cleanup is ready.",
        )

    def run_consolidation(self, timeout_s=120.0):
        from meeting.agent.scheduler import ConsolidationOutcome

        self.consolidations += 1
        return ConsolidationOutcome(
            status="completed",
            message="Final cloud insights are ready.",
        )

# Fixtures

@pytest.fixture
def db(tmp_path):
    from services.database import DatabaseManager
    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    yield manager
    manager.close()

@pytest.fixture
def repo(db):
    from meeting.persist.repository import SqlMeetingRepository
    return SqlMeetingRepository(db=db)

@pytest.fixture
def fakes(monkeypatch):
    """Install fake lazy-import targets for every engine subsystem.

    Returns a namespace with the stub modules (so a test can swap a class),
    the agent cores handed out by the patched factory, and the diarizer.
    """
    FakeAsr.instances = []
    FakeServer.instances = []
    FakeScheduler.instances = []

    capture_devices = types.ModuleType("meeting.capture.devices")
    capture_devices.find_mic_device = lambda device_id=None: {
        "index": 1, "samplerate": 16000, "channels": 1,
    }
    capture_devices.find_loopback_device = lambda: {
        "index": 2, "samplerate": 16000, "channels": 2,
    }

    sd_stream = types.ModuleType("meeting.capture.sd_stream")
    sd_stream.SdCaptureSource = FakeSource

    soundcard_stream = types.ModuleType("meeting.capture.soundcard_stream")
    sck_stream = types.ModuleType("meeting.capture.sck_stream")

    class _UnavailableLoopback:
        @staticmethod
        def available():
            return False

    soundcard_stream.SoundcardLoopbackSource = _UnavailableLoopback
    sck_stream.ScreenCaptureKitLoopbackSource = _UnavailableLoopback

    spool = types.ModuleType("meeting.capture.spool")
    spool.SpoolWriter = FakeSpool
    spool.QUIET_RMS = 300.0
    spool.QUIET_WINDOW_S = 0.4
    spool.find_cut_point = lambda *args, **kwargs: None
    spool.load_session_meta = lambda *args, **kwargs: None
    spool.resolve_session_wav = lambda *args, **kwargs: None
    spool.session_meta_path = lambda *args, **kwargs: ""

    asr_engine = types.ModuleType("meeting.asr.engine")
    asr_engine.MeetingAsrEngine = FakeAsr

    asr_audio = types.ModuleType("meeting.asr.audio")
    asr_audio.WHISPER_SAMPLE_RATE = 16000
    asr_audio.load_wav_int16 = lambda path: (
        np.zeros(16000 * 10, dtype=np.int16), 16000,
    )
    asr_audio.prepare_for_whisper = lambda frames, rate: (
        frames.astype(np.float32) / 32768.0
    )

    web_server = types.ModuleType("meeting.web.server")
    web_server.MeetingWebServer = FakeServer

    diarizer = FakeDiarizer()
    clustering = types.ModuleType("meeting.diarize.clustering")
    clustering.create_diarizer = (
        lambda model_path, store, repository, meeting_id: diarizer
    )

    modules = {
        "meeting.capture.devices": capture_devices,
        "meeting.capture.sd_stream": sd_stream,
        "meeting.capture.soundcard_stream": soundcard_stream,
        "meeting.capture.sck_stream": sck_stream,
        "meeting.capture.spool": spool,
        "meeting.asr.engine": asr_engine,
        "meeting.asr.audio": asr_audio,
        "meeting.web.server": web_server,
        "meeting.diarize.clustering": clustering,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    cores = []
    ns = types.SimpleNamespace(
        modules=modules, cores=cores, diarizer=diarizer,
        core_healthy=True,
        asr=FakeAsr.instances, servers=FakeServer.instances,
        schedulers=FakeScheduler.instances,
    )

    def create_agent_core(kind, payload_dir=None):
        core = FakeAgentCore(healthy=ns.core_healthy)
        cores.append(core)
        return core

    monkeypatch.setattr("meeting.agent.base.create_agent_core",
                        create_agent_core, raising=True)
    monkeypatch.setattr("meeting.agent.scheduler.CheckpointScheduler",
                        FakeScheduler, raising=True)

    return ns

@pytest.fixture
def make_engine(repo, tmp_path, fakes):
    """Factory building engines wired to the fakes, torn down after the test."""
    from meeting.engine import MeetingEngine, MeetingEngineOptions

    engines = []

    def build(cloud_enabled=True, **overrides):
        options = MeetingEngineOptions(
            title="Test meeting",
            cloud_enabled=cloud_enabled,
            spool_root=str(tmp_path / "spool"),
            agent_core_kind="direct",
            **overrides,
        )
        engine = MeetingEngine(options, repository=repo)
        engine.events = []
        engine.add_listener(
            lambda kind, payload: engine.events.append((kind, dict(payload)))
        )
        engines.append(engine)
        return engine

    yield build

    for engine in engines:
        try:
            engine.shutdown()
        except Exception:
            pass
        engine._stop_heartbeat()

def events_of(engine, kind):
    """All payloads emitted by ``engine`` for one listener event kind."""
    return [payload for k, payload in engine.events if k == kind]

def loopback_segment(meeting_id, seg_id, start=1.0, end=3.0, chunk_id=None):
    return TranscriptSegment(
        segment_id=seg_id, meeting_id=meeting_id, chunk_id=chunk_id,
        channel="loopback", start_s=start, end_s=end, text="hello there",
    )

# Required startup services

class TestRequiredStartupServices:
    def test_server_failure_aborts_and_discards_empty_meeting(
            self, make_engine, repo, fakes):
        class FailingServer(FakeServer):
            def start(self):
                raise ModuleNotFoundError(
                    "Meeting Mode needs the dashboard dependencies "
                    "(missing module: uvicorn)."
                )

        fakes.modules["meeting.web.server"].MeetingWebServer = FailingServer
        engine = make_engine(cloud_enabled=False)

        with pytest.raises(RuntimeError, match="dashboard dependencies"):
            engine.start()

        assert engine.is_active() is False
        assert repo.get_meeting(engine.meeting_id) is None
        assert all("server_started" != kind for kind, _ in engine.events)

    def test_zero_capture_sources_fail_before_dashboard_start(
            self, make_engine, repo, fakes):
        devices = fakes.modules["meeting.capture.devices"]
        devices.find_mic_device = lambda device_id=None: None
        devices.find_loopback_device = lambda: None
        engine = make_engine(cloud_enabled=False)

        with pytest.raises(RuntimeError, match="No audio devices"):
            engine.start()

        assert engine.is_active() is False
        assert repo.get_meeting(engine.meeting_id) is None
        assert fakes.servers == []

    def test_missing_loopback_still_allows_mic_only_start(
            self, make_engine, fakes):
        fakes.modules["meeting.capture.devices"].find_loopback_device = lambda: None
        engine = make_engine(cloud_enabled=False)

        result = engine.start()

        assert result["host_url"]
        assert engine.is_active() is True
        assert {source.channel for source in engine._sources} == {"mic"}


# Intelligence health

class TestIntelligenceHealth:
    def test_unhealthy_core_reports_offline_and_meeting_still_starts(
            self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True)
        fakes.core_healthy = False

        result = engine.start()

        assert result["meeting_id"]
        # The meeting itself is unaffected: transcript-only, still active.
        assert engine.is_active()
        assert fakes.asr and fakes.asr[0].on_segments is not None
        assert engine._scheduler is None

        online = engine.store.with_state(lambda s: s.intelligence_online)
        assert online is False

        intel = events_of(engine, "intelligence")
        assert intel and intel[-1]["online"] is False
        assert intel[-1].get("error")  # a reason is reported, not silence

        statuses = events_of(engine, "status")
        assert statuses[-1]["intelligence_online"] is False
        # The unusable core is released so a later retry rebuilds it.
        assert fakes.cores[0].shutdowns == 1
        assert engine._agent_core is None

    def test_start_seeds_report_views(self, make_engine, fakes):
        default_engine = make_engine(cloud_enabled=True)
        default_engine.start()
        assert default_engine.store.with_state(lambda s: s.report_views) == [
            "ribbon", "brief", "signal",
        ]

        engine = make_engine(
            cloud_enabled=True,
            report_views=("ribbon",),
        )
        engine.start()
        assert engine.store.with_state(lambda s: s.report_views) == ["ribbon"]

    def test_healthy_core_reports_online(self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()

        assert engine.store.with_state(lambda s: s.intelligence_online) is True
        assert events_of(engine, "intelligence")[-1]["online"] is True
        assert fakes.schedulers and fakes.schedulers[0].started

    def test_start_persists_and_passes_llm_endpoint(
            self, make_engine, fakes, repo):
        import json

        endpoint = {
            "profile_id": "custom_abcd1234",
            "name": "LM Studio",
            "kind": "custom",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key_env": "",
        }
        engine = make_engine(
            cloud_enabled=True,
            llm_provider="custom_abcd1234",
            llm_model="local-qwen",
            llm_endpoint=endpoint,
        )
        result = engine.start()
        meeting = repo.get_meeting(result["meeting_id"])
        stored = meeting["agent_endpoint_json"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["base_url"] == endpoint["base_url"]
        assert fakes.cores[0].config.endpoint["base_url"] == endpoint["base_url"]
        assert fakes.cores[0].config.provider == "custom_abcd1234"

    def test_cloud_disabled_starts_no_intelligence(self, make_engine, fakes):
        engine = make_engine(cloud_enabled=False)
        engine.start()

        assert fakes.cores == []
        assert fakes.schedulers == []
        assert fakes.asr[0].enable_revisions is False
        assert engine.store.with_state(lambda s: s.intelligence_online) is False

    def test_asr_receives_pinned_meeting_language(self, make_engine, fakes):
        engine = make_engine(cloud_enabled=False, asr_language="en")

        engine.start()

        assert fakes.asr[0].language == "en"

    def test_scheduler_health_signal_is_wired_and_propagates(
            self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()

        scheduler = fakes.schedulers[0]
        assert callable(scheduler.on_health), (
            "CheckpointScheduler must receive an on_health callback"
        )

        before = len(events_of(engine, "intelligence"))
        scheduler.on_health(False)

        assert engine.store.with_state(lambda s: s.intelligence_online) is False
        intel = events_of(engine, "intelligence")
        assert len(intel) == before + 1
        assert intel[-1]["online"] is False
        assert intel[-1].get("error")
        # Dashboard clients learn about it too.
        broadcasts = [
            m for m in fakes.servers[0].messages
            if m.get("type") == "status" and m.get("intelligence_online") is False
        ]
        assert broadcasts

        scheduler.on_health(True)
        assert engine.store.with_state(lambda s: s.intelligence_online) is True

class TestCloudToggle:
    def test_stop_intelligence_shuts_the_agent_core_down(
            self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()
        core = fakes.cores[0]
        scheduler = fakes.schedulers[0]

        engine.set_cloud_enabled(False)

        assert scheduler.stopped
        assert core.cancels == 1
        assert core.shutdowns == 1, (
            "revoking consent must not leave the agent process alive"
        )
        assert engine._agent_core is None
        assert engine.store.with_state(lambda s: s.intelligence_online) is False
        assert events_of(engine, "intelligence")[-1]["online"] is False

    def test_reenabling_does_not_resend_the_whole_transcript(
            self, make_engine, fakes, repo):
        engine = make_engine(cloud_enabled=True)
        engine.start()
        meeting_id = engine.meeting_id
        repo.add_segments([
            loopback_segment(meeting_id, "sg_a", 1.0, 3.0),
            loopback_segment(meeting_id, "sg_b", 4.0, 6.0),
        ])

        engine.set_cloud_enabled(False)
        engine.set_cloud_enabled(True)

        assert len(fakes.schedulers) == 2
        seeded = fakes.schedulers[1].seeded
        assert seeded is not None, "the rebuilt scheduler was not seeded"
        assert {s["id"] for s in seeded} == {"sg_a", "sg_b"}
        # A brand-new core is built, since the old one was shut down.
        assert len(fakes.cores) == 2
        assert engine.store.with_state(lambda s: s.intelligence_online) is True

# End / lifecycle

class TestEndLifecycle:
    def test_end_failure_still_emits_ended(self, make_engine, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        meeting_id = engine.meeting_id

        def boom():
            raise RuntimeError("spool flush exploded")

        engine._flush_spools = boom

        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert not engine._end_thread.is_alive()

        errors = events_of(engine, "error")
        assert errors and errors[-1]["code"] == "end_failed"
        ended = events_of(engine, "ended")
        assert ended, "a failed end must still emit 'ended' so the app unwinds"
        assert ended[-1]["meeting_id"] == meeting_id
        assert engine.is_active() is False
        assert repo.get_meeting(meeting_id)["status"] == "needs_recovery"

    def test_normal_end_emits_ended_and_marks_the_meeting(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()
        meeting_id = engine.meeting_id

        engine.end()
        engine._end_thread.join(timeout=10.0)

        assert events_of(engine, "ended")[-1]["canceled"] is False
        assert repo.get_meeting(meeting_id)["status"] == "ended"
        assert fakes.schedulers[0].consolidations == 1
        assert fakes.cores[0].shutdowns == 1

    def test_end_event_precedes_slow_consolidation(
            self, make_engine, repo, fakes):
        from meeting.agent.scheduler import ConsolidationOutcome

        engine = make_engine(cloud_enabled=True)
        engine.start()
        scheduler = fakes.schedulers[0]
        consolidation_entered = threading.Event()
        consolidation_release = threading.Event()

        def slow_consolidation(timeout_s=120.0):
            scheduler.consolidations += 1
            consolidation_entered.set()
            consolidation_release.wait(timeout=5.0)
            return ConsolidationOutcome(
                status="completed",
                message="Final cloud insights are ready.",
            )

        scheduler.run_consolidation = slow_consolidation
        try:
            engine.end()
            assert consolidation_entered.wait(timeout=5.0)

            ended = events_of(engine, "ended")
            assert ended, "end must be published before final insight polish"
            assert engine.is_active() is False
            assert repo.get_meeting(engine.meeting_id)["status"] == "ended"
            # running must be persisted/broadcast before the ended event while
            # consolidation is still blocked.
            fin = engine.store.with_state(lambda s: s.finalization.to_dict())
            assert fin["status"] == "running"
            status_events = events_of(engine, "status")
            assert any(
                (e.get("finalization") or {}).get("status") == "running"
                for e in status_events
            )
        finally:
            consolidation_release.set()
            engine._end_thread.join(timeout=10.0)

        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "completed"
        assert repo.get_meeting(engine.meeting_id)["status"] == "ended"

    def test_end_disabled_cloud_finalization(self, make_engine, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "disabled"
        assert repo.get_meeting(engine.meeting_id)["status"] == "ended"

    def test_end_unhealthy_intelligence_finalization(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()
        fakes.cores[0].healthy = False
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "unavailable"
        assert repo.get_meeting(engine.meeting_id)["status"] == "ended"

    def test_end_needs_recovery_blocks_consolidation(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()

        def unfinished(_meeting_id):
            return 2

        engine.repository.count_unfinished_chunks = unfinished
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert fakes.schedulers[0].consolidations == 0
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "unavailable"
        assert "recovery" in fin["message"].lower()
        assert repo.get_meeting(engine.meeting_id)["status"] == "needs_recovery"

    def test_end_failed_consolidation_outcome(self, make_engine, repo, fakes):
        from meeting.agent.scheduler import ConsolidationOutcome

        engine = make_engine(cloud_enabled=True)
        engine.start()
        scheduler = fakes.schedulers[0]

        def boom(timeout_s=120.0):
            scheduler.consolidations += 1
            return ConsolidationOutcome(
                status="failed", message="Final cloud insights failed: boom",
            )

        scheduler.run_consolidation = boom
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "failed"
        assert repo.get_meeting(engine.meeting_id)["status"] == "ended"

    def test_end_failed_polish_marks_step_failed(self, make_engine, repo, fakes):
        from meeting.agent.scheduler import ConsolidationOutcome

        engine = make_engine(cloud_enabled=True)
        engine.start()
        scheduler = fakes.schedulers[0]

        def boom(timeout_s=60.0, progress_cb=None):
            scheduler.polishes += 1
            return ConsolidationOutcome(
                status="failed", message="Transcript cleanup failed: boom",
            )

        scheduler.run_final_polish = boom
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        polish = next(step for step in fin["steps"] if step["id"] == "polish")
        assert polish["status"] == "failed"
        assert fin["status"] == "failed"
        assert scheduler.consolidations == 1

    def test_ended_precedes_slow_offline_pass(self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=False, end_redecode=True)
        engine.start()
        asr = fakes.asr[0]
        entered = threading.Event()
        release = threading.Event()

        def slow_offline(spool_dir, chunks=None):
            asr.offline_passes += 1
            entered.set()
            release.wait(timeout=5.0)
            return []

        asr.transcribe_offline_session = slow_offline
        try:
            engine.end()
            assert entered.wait(timeout=5.0)
            ended = events_of(engine, "ended")
            assert ended, "ended must fire before the offline re-decode"
            assert engine.is_active() is False
            assert repo.get_meeting(engine.meeting_id)["status"] == "ended"
        finally:
            release.set()
            engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "disabled"

    def test_cloud_off_keeps_draft_when_offline_commit_disabled(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        meeting_id = engine.meeting_id
        repo.add_segments([
            TranscriptSegment(
                segment_id="sg_draft", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=1.0, text="draft",
            )
        ])
        asr = fakes.asr[0]
        asr.offline_segments = [
            TranscriptSegment(
                segment_id="sg_clean", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=1.2, text="clean",
            )
        ]
        engine.end()
        engine._end_thread.join(timeout=10.0)
        ids = {row["id"] for row in repo.get_segments(meeting_id)}
        assert "sg_draft" in ids
        assert "sg_clean" not in ids
        assert asr.offline_passes == 0
        assert not fakes.schedulers
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "disabled"

    def test_cloud_off_replaces_transcript_when_redecode_enabled(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=False, end_redecode=True)
        engine.start()
        meeting_id = engine.meeting_id
        repo.add_segments([
            TranscriptSegment(
                segment_id="sg_draft", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=1.0, text="draft",
            )
        ])
        asr = fakes.asr[0]
        asr.offline_segments = [
            TranscriptSegment(
                segment_id="sg_clean", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=1.2, text="clean",
            )
        ]
        engine.end()
        engine._end_thread.join(timeout=10.0)
        ids = {row["id"] for row in repo.get_segments(meeting_id)}
        assert "sg_clean" in ids
        assert "sg_draft" not in ids
        assert not fakes.schedulers
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "disabled"

    def test_cloud_on_polishes_draft_without_offline_replace(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True)
        engine.start()
        meeting_id = engine.meeting_id
        repo.add_segments([
            TranscriptSegment(
                segment_id="sg_draft", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=1.0, text="draft",
            )
        ])
        asr = fakes.asr[0]
        asr.offline_segments = [
            TranscriptSegment(
                segment_id="sg_final", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=2.0, text="final",
            )
        ]
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert asr.offline_passes == 0
        assert fakes.schedulers[0].polishes == 1
        assert fakes.schedulers[0].consolidations == 1
        ids = {row["id"] for row in repo.get_segments(meeting_id)}
        assert "sg_draft" in ids
        assert "sg_final" not in ids
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "completed"

    def test_offline_redecode_runs_polish_then_consolidation(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True, end_redecode=True)
        engine.start()
        meeting_id = engine.meeting_id
        asr = fakes.asr[0]
        asr.offline_segments = [
            TranscriptSegment(
                segment_id="sg_final", meeting_id=meeting_id, chunk_id=None,
                channel="mic", start_s=0.0, end_s=2.0, text="final",
            )
        ]
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert asr.offline_passes == 1
        assert fakes.schedulers[0].polishes == 1
        assert fakes.schedulers[0].consolidations == 1
        ids = {row["id"] for row in repo.get_segments(meeting_id)}
        assert "sg_final" in ids
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "completed"

    def test_end_skips_polish_when_disabled(self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True, end_polish=False, end_report=True)
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert fakes.schedulers[0].polishes == 0
        assert fakes.schedulers[0].consolidations == 1

    def test_end_skips_report_when_disabled(self, make_engine, fakes):
        engine = make_engine(cloud_enabled=True, end_polish=True, end_report=False)
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert fakes.schedulers[0].polishes == 1
        assert fakes.schedulers[0].consolidations == 0
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "completed"
        assert "cleanup" in (fin.get("message") or "").lower()

    def test_end_skips_llm_when_both_disabled(self, make_engine, fakes):
        engine = make_engine(
            cloud_enabled=True, end_polish=False, end_report=False,
        )
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        assert fakes.schedulers[0].polishes == 0
        assert fakes.schedulers[0].consolidations == 0
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        assert fin["status"] == "completed"

    def test_end_racing_start_waits_for_the_pipeline(self, make_engine, fakes):
        entered = threading.Event()
        release = threading.Event()

        class BlockingAsr(FakeAsr):
            def __init__(self, model, meeting_id, repository, language=None,
                         enable_revisions=True):
                super().__init__(
                    model,
                    meeting_id,
                    repository,
                    language=language,
                    enable_revisions=enable_revisions,
                )
                entered.set()
                release.wait(timeout=10.0)

        fakes.modules["meeting.asr.engine"].MeetingAsrEngine = BlockingAsr

        engine = make_engine(cloud_enabled=False)
        starter = threading.Thread(target=engine.start, name="test-start")
        starter.start()
        try:
            assert entered.wait(timeout=5.0)
            ender = threading.Thread(target=engine.end, name="test-end")
            ender.start()
            try:
                # The end must not run against the half-built pipeline.
                ender.join(timeout=0.5)
                assert engine._end_thread is None
                assert ender.is_alive()
            finally:
                release.set()
                ender.join(timeout=10.0)
        finally:
            release.set()
            starter.join(timeout=10.0)

        assert not starter.is_alive()
        end_thread = engine._end_thread
        assert end_thread is not None
        end_thread.join(timeout=10.0)
        assert events_of(engine, "ended")
        # The pipeline that start() built was fully torn down by the end.
        assert engine.is_active() is False
        assert fakes.servers[0].stopped is False  # server outlives end()

    def test_double_end_reuses_the_same_worker(self, make_engine):
        engine = make_engine(cloud_enabled=False)
        engine.start()

        engine.end()
        first = engine._end_thread
        engine.end()
        assert engine._end_thread is first
        first.join(timeout=10.0)
        assert len(events_of(engine, "ended")) == 1

# Capture recovery

class TestCaptureRecovery:
    def test_default_loopback_change_restarts_only_that_channel(
            self, make_engine, fakes, monkeypatch):
        import meeting.engine as engine_module

        loopback_index = [2]
        fakes.modules["meeting.capture.devices"].find_loopback_device = lambda: {
            "index": loopback_index[0], "samplerate": 16000, "channels": 2,
        }
        monkeypatch.setattr(engine_module, "CAPTURE_WATCHDOG_INTERVAL_S", 0.01)
        monkeypatch.setattr(engine_module, "CAPTURE_RETRY_INTERVAL_S", 0.0)
        engine = make_engine(cloud_enabled=False)
        engine.start()
        original_mic = engine._capture_source("mic")
        original_loopback = engine._capture_source("loopback")

        loopback_index[0] = 9
        deadline = time.monotonic() + 2.0
        replacement = original_loopback
        while time.monotonic() < deadline:
            candidate = engine._capture_source("loopback")
            if candidate is not None and candidate is not original_loopback:
                replacement = candidate
                break
            time.sleep(0.01)

        assert replacement is not original_loopback
        assert replacement.device_id == 9
        assert original_loopback.stopped is True
        assert engine._capture_source("mic") is original_mic
        assert original_mic.is_active() is True
        capture = engine.store.snapshot()["capture"]
        assert capture["mic_available"] is True
        assert capture["loopback_available"] is True
        assert capture["message"] == ""

# Diarization degradation

class TestDiarizationDegradation:
    def _prime(self, engine, repo):
        """Register a chunk the loopback segments can be sliced out of."""
        seq = repo.next_chunk_seq(engine.meeting_id, "loopback")
        chunk_id = repo.register_chunk(
            meeting_id=engine.meeting_id, channel="loopback", seq=seq,
            file_path=os.devnull, start_s=0.0, duration_s=10.0,
            sample_rate=16000,
        )
        engine._chunk_index[chunk_id] = SpooledChunk(
            chunk_id=chunk_id, meeting_id=engine.meeting_id,
            channel="loopback", seq=seq, file_path=os.devnull, start_s=0.0,
            duration_s=10.0, sample_rate=16000,
        )
        return chunk_id

    def test_none_assignment_while_available_keeps_diarization_on(
            self, make_engine, fakes, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        chunk_id = self._prime(engine, repo)
        fakes.diarizer.next_participant = None  # short segment: normal None

        engine._on_chunk_result(engine._chunk_index[chunk_id], [
            loopback_segment(engine.meeting_id, "sg_short", chunk_id=chunk_id),
        ])

        assert engine.store.with_state(lambda s: s.diarization_available) is True
        assert engine._degraded_diarization is False
        assert repo.get_segments(engine.meeting_id)  # really persisted

    def test_degradation_flips_availability_once(self, make_engine, fakes, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        chunk_id = self._prime(engine, repo)
        assert engine.store.with_state(lambda s: s.diarization_available) is True

        fakes.diarizer.available = False
        fakes.diarizer.next_participant = None

        engine._on_chunk_result(engine._chunk_index[chunk_id], [
            loopback_segment(engine.meeting_id, "sg_1", chunk_id=chunk_id),
        ])
        # A second chunk is needed because chunk commits are idempotent.
        second_id = self._prime(engine, repo)
        engine._on_chunk_result(engine._chunk_index[second_id], [
            loopback_segment(engine.meeting_id, "sg_2", 4.0, 6.0,
                             chunk_id=second_id),
        ])

        assert engine.store.with_state(lambda s: s.diarization_available) is False
        assert engine._degraded_diarization is True

        notices = [
            payload for payload in events_of(engine, "status")
            if payload.get("diarization_available") is False
        ]
        assert len(notices) == 1
        assert notices[0]["diarization_available"] is False

        broadcasts = [
            m for m in fakes.servers[0].messages
            if m.get("type") == "status"
            and m.get("diarization_available") is False
        ]
        assert len(broadcasts) == 1
        # Only probed until it latched; the second batch does not re-probe.
        assert fakes.diarizer.availability_probes == 2  # startup + degradation
        # The diarizer object stays so human corrections still reach pin().
        assert engine._diarizer is fakes.diarizer

    def test_pin_still_works_after_degradation(self, make_engine, fakes, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        chunk_id = self._prime(engine, repo)
        repo.add_segments([loopback_segment(engine.meeting_id, "sg_p")])
        fakes.diarizer.available = False
        engine._on_chunk_result(engine._chunk_index[chunk_id], [
            loopback_segment(engine.meeting_id, "sg_1", chunk_id=chunk_id),
        ])
        assert engine._degraded_diarization is True

        participant = engine.add_guest("Riley")
        results = engine.apply_client_action("host", None, {
            "op": "reassign_segment_speaker", "segment_id": "sg_p",
            "participant_id": participant["id"],
        })

        assert results[0].ok, results[0].reason
        assert fakes.diarizer.pins == [("sg_p", participant["id"])]

# Store wiring

class TestStoreWiring:
    def test_pinned_segments_are_protected_from_the_diarizer(
            self, make_engine, repo):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        repo.add_segments([loopback_segment(engine.meeting_id, "sg_pinned")])
        participant = engine.add_guest("Alex")
        human = engine.apply_client_action("host", None, {
            "op": "reassign_segment_speaker", "segment_id": "sg_pinned",
            "participant_id": participant["id"],
        })
        assert human[0].ok

        other = engine.add_guest("Sam")
        [rejected] = engine.store.apply("system", "diarizer", [{
            "op": "reassign_segment_speaker", "segment_id": "sg_pinned",
            "participant_id": other["id"],
        }])

        assert rejected.ok is False
        assert rejected.reason == "segment_pinned"
        assert repo.get_segment(
            engine.meeting_id, "sg_pinned"
        )["speaker_participant_id"] == (
            participant["id"]
        )

class TestDemoMeeting:
    def test_demo_mode_skips_live_audio_and_seeds_transcript(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True, demo_mode=True)
        result = engine.start()

        assert result["meeting_id"]
        assert engine.is_active()
        assert engine.options.demo_mode is True
        assert fakes.asr == []
        assert engine._sources == []
        assert engine._asr is None
        assert engine._diarizer is None
        assert fakes.schedulers and fakes.schedulers[0].started

        segments = repo.get_segments(engine.meeting_id)
        assert len(segments) >= 10
        assert any("June" in (row.get("text") or "") for row in segments)

        snapshot = engine.store.snapshot()
        assert snapshot["title"]
        assert snapshot["topic"]["current"]
        assert snapshot["cards"]["key_points"]
        assert snapshot["cards"]["decisions"]
        assert snapshot["capture"]["mic_available"] is False
        assert snapshot["capture"]["loopback_available"] is False

    def test_demo_mode_end_runs_polish_and_report(
            self, make_engine, repo, fakes):
        engine = make_engine(cloud_enabled=True, demo_mode=True)
        engine.start()
        meeting_id = engine.meeting_id

        engine.end()
        engine._end_thread.join(timeout=10.0)

        assert events_of(engine, "ended")[-1]["status"] == "ended"
        assert repo.get_meeting(meeting_id)["status"] == "ended"
        assert fakes.schedulers[0].polishes == 1
        assert fakes.schedulers[0].consolidations == 1
        assert engine.store.with_state(
            lambda s: s.finalization.status
        ) == "completed"

class TestCloudSpeakerStep:
    def test_local_backend_omits_speaker_step(self, make_engine):
        engine = make_engine(cloud_enabled=False)
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        ids = [step["id"] for step in fin.get("steps") or []]
        assert "speaker_id" not in ids
        assert fin["status"] == "disabled"

    def test_openai_without_consent_skips_honestly(self, make_engine):
        engine = make_engine(
            cloud_enabled=False,
            speaker_id_backend="openai",
            speaker_id_audio_consent=False,
        )
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        steps = {step["id"]: step for step in fin.get("steps") or []}
        assert "speaker_id" in steps
        assert steps["speaker_id"]["status"] == "completed"
        assert "consent" in (steps["speaker_id"].get("detail") or "").lower()

    def test_openai_pass_success(self, make_engine):
        engine = make_engine(
            cloud_enabled=False,
            speaker_id_backend="openai",
            speaker_id_audio_consent=True,
        )
        engine._run_cloud_speaker_pass = lambda **_kw: {
            "ok": True, "skipped": False, "applied": 3, "error": None,
        }
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        steps = {step["id"]: step for step in fin.get("steps") or []}
        assert steps["speaker_id"]["status"] == "completed"
        assert fin["status"] == "completed"

    def test_openai_pass_failure_does_not_break_end(self, make_engine, repo):
        engine = make_engine(
            cloud_enabled=False,
            speaker_id_backend="openai",
            speaker_id_audio_consent=True,
        )

        def boom(**_kw):
            raise RuntimeError("api down")

        engine._run_cloud_speaker_pass = boom
        engine.start()
        engine.end()
        engine._end_thread.join(timeout=10.0)
        fin = engine.store.with_state(lambda s: s.finalization.to_dict())
        steps = {step["id"]: step for step in fin.get("steps") or []}
        assert steps["speaker_id"]["status"] == "failed"
        assert repo.get_meeting(engine.meeting_id)["status"] == "ended"

def test_engine_module_has_no_dead_recent_text_api():
    """The unused topic-shift buffer is gone (the scheduler reads the DB)."""
    from meeting.engine import MeetingEngine

    assert not hasattr(MeetingEngine, "get_recent_text")

class TestDirectAgentCapabilities:
    def test_json_mode_retries_without_response_format(self):
        from unittest.mock import MagicMock

        from meeting.agent.openrouter_direct import DirectOpenRouterAgent

        agent = DirectOpenRouterAgent()
        agent._model = "local-qwen"
        agent._use_json_response_format = True
        agent._tools = MagicMock()
        agent._tools.apply_agent_ops.return_value = []
        client = MagicMock()
        timed = MagicMock()
        client.with_options.return_value = timed

        def _create(**kwargs):
            if "response_format" in kwargs:
                raise RuntimeError("unknown field response_format / json_object")
            return MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content='{"ops": []}'))
                ],
                usage=None,
            )

        timed.chat.completions.create.side_effect = _create
        result = agent._run_json_mode(client, "sys", "user", timeout_s=5)
        assert result.ok is True
        assert agent._use_json_response_format is False
        assert timed.chat.completions.create.call_count == 2
