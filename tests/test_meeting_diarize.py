"""
Tests for diarization: pin preservation, bounded re-clustering, graceful
degradation, and Kaldi-compatible filterbank features.
"""
import logging
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.diarize import fbank as fbank_mod
from meeting.diarize.clustering import (
    MAX_WORKING_SET,
    MIN_SPLIT_SEGMENTS,
    PIN_WEIGHT,
    RECLUSTER_WINDOW,
    OnlineDiarizer,
    _Cluster,
    _SegRecord,
    _average_linkage,
)
from meeting.interfaces import TranscriptSegment, OpResult


def _unit(vec):
    a = np.asarray(vec, dtype=np.float32)
    return (a / np.linalg.norm(a)).astype(np.float32)


class FakeEmbedder:
    def __init__(self, vectors):
        self._vectors = list(vectors)
        self._i = 0
        self.available = True

    def embed(self, mono):
        if self._i >= len(self._vectors):
            return self._vectors[-1].copy()
        v = self._vectors[self._i]
        self._i += 1
        return v.copy()


class RaisingEmbedder:
    """Embedder whose ONNX session fails at runtime (mid-meeting failure)."""

    def __init__(self):
        self.calls = 0
        self.available = True

    def embed(self, mono):
        self.calls += 1
        raise RuntimeError("onnxruntime session crashed")


class FakeStore:
    def __init__(self):
        self.n = 0

    def apply(self, actor_type, actor_id, ops):
        self.n += 1
        pid = f"p_speaker_{self.n}"
        return [OpResult(
            ok=True, op=ops[0], target_id=pid,
            effect={"participant": {"id": pid, "display_name": f"Speaker {self.n}"}},
        )]


class FakeRepo:
    def __init__(self):
        self.embeddings = {}

    def set_segment_embedding(self, segment_id, payload):
        self.embeddings[segment_id] = payload


def _make_diarizer(vectors=None, embedder=None):
    embedder = embedder or FakeEmbedder(vectors or [])
    d = OnlineDiarizer(embedder, FakeStore(), FakeRepo(), "m_diar")
    return d


def _segment(seg_id, t0, channel="loopback"):
    return TranscriptSegment(
        segment_id=seg_id, meeting_id="m_diar", chunk_id=1, channel=channel,
        start_s=t0, end_s=t0 + 1.0, text="x",
    )


def _synthetic_speakers(n, dim=192, speakers=4, sigma=0.035, seed=7):
    """Unit embeddings for ``speakers`` voices that genuinely cluster.

    Within-speaker cosine distance lands near 0.19 and between-speaker near
    1.0, comfortably straddling the 0.38 merge threshold.
    """
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(speakers, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    out = []
    for i in range(n):
        v = centers[i % speakers] + sigma * rng.normal(size=dim)
        out.append((v / np.linalg.norm(v)).astype(np.float32))
    return out


def _prefill(diarizer, vectors, pid_of=lambda i: f"p_{i % 4}"):
    """Populate a diarizer's records directly (skips the online pass)."""
    records = [
        _SegRecord(f"sg_{i}", vec, pid_of(i), False, float(i), float(i) + 1.0)
        for i, vec in enumerate(vectors)
    ]
    diarizer._records = list(records)
    diarizer._by_segment_id = {r.segment_id: r for r in records}
    return records


class TestAverageLinkage:
    def test_separates_distant_pairs(self):
        # Two tight pairs far apart
        emb = np.stack([
            _unit([1, 0, 0]),
            _unit([0.99, 0.1, 0]),
            _unit([0, 1, 0]),
            _unit([0.1, 0.99, 0]),
        ])
        dist = np.clip(1.0 - emb @ emb.T, 0.0, 2.0)
        groups = _average_linkage(dist, threshold=0.38)
        assert len(groups) == 2
        flat = sorted(sorted(g) for g in groups)
        assert flat == [[0, 1], [2, 3]]


class TestPinPreservation:
    def test_pinned_segments_never_relabeled_on_recluster(self):
        d = _make_diarizer()
        # Speaker A / B axes; pin one A-ish embedding to participant B so a
        # naive recluster would want to flip it — pin must win.
        a1 = _unit([1.0, 0.0, 0.0])
        a2 = _unit([0.95, 0.05, 0.0])
        b1 = _unit([0.0, 1.0, 0.0])
        b2 = _unit([0.05, 0.95, 0.0])
        # Mis-assigned: embedding looks like A but labeled/pinned as B
        pinned_emb = _unit([0.98, 0.02, 0.0])

        records = [
            _SegRecord("sg_a1", a1, "p_A", False, 0.0, 1.0),
            _SegRecord("sg_a2", a2, "p_A", False, 1.0, 2.0),
            _SegRecord("sg_b1", b1, "p_B", False, 2.0, 3.0),
            _SegRecord("sg_b2", b2, "p_B", False, 3.0, 4.0),
            _SegRecord("sg_pin", pinned_emb, "p_B", True, 4.0, 5.0),
        ]
        d._records = list(records)
        d._by_segment_id = {r.segment_id: r for r in records}
        d._clusters = [
            _Cluster("p_A", a1.copy(), count=2),
            _Cluster("p_B", b1.copy(), count=3, pinned_segments={"sg_pin"}),
        ]
        d._new_since_recluster = 25

        ops = d._recluster()
        # Pinned segment keeps p_B
        assert d._by_segment_id["sg_pin"].participant_id == "p_B"
        assert d._by_segment_id["sg_pin"].pinned is True
        # No relabel op targets the pinned segment
        assert all(op["segment_id"] != "sg_pin" for op in ops)

    def test_pin_api_marks_segment_and_blocks_relabel(self):
        vectors = [
            _unit([1, 0, 0]),
            _unit([0.9, 0.1, 0]),
            _unit([0, 1, 0]),
            _unit([0.1, 0.9, 0]),
        ]
        d = _make_diarizer(vectors)
        relabels = []
        d.set_relabel_callback(relabels.extend)

        def seg(i, t0):
            return TranscriptSegment(
                segment_id=f"sg_{i}", meeting_id="m_diar", chunk_id=1,
                channel="loopback", start_s=t0, end_s=t0 + 1.0, text="x",
            )

        audio = np.zeros(16000, dtype=np.float32)
        pids = []
        for i in range(4):
            pids.append(d.assign(seg(i, float(i)), audio, 16000))

        # Pin first segment to the second speaker cluster
        target = pids[2]
        d.pin("sg_0", target)
        assert d._by_segment_id["sg_0"].pinned is True
        assert d._by_segment_id["sg_0"].participant_id == target

        # Force recluster with enough members
        d._new_since_recluster = 25
        ops = d._recluster()
        assert d._by_segment_id["sg_0"].participant_id == target
        assert all(op["segment_id"] != "sg_0" for op in ops)

    def test_pin_weight_constant(self):
        assert PIN_WEIGHT == 100


class TestPinRaceProtection:
    """A pin landing between op computation and dispatch must win."""

    def _racing_diarizer(self):
        vectors = _synthetic_speakers(8, speakers=2)
        d = _make_diarizer(vectors)
        # Two speakers, but everything is mislabeled onto one participant so
        # the re-cluster genuinely wants to move segments.
        records = _prefill(d, vectors, pid_of=lambda i: "p_A")
        d._clusters = [_Cluster("p_A", vectors[0].copy(), count=len(records))]
        return d

    def test_stale_op_for_pinned_segment_is_filtered_before_dispatch(self):
        d = self._racing_diarizer()
        delivered = []
        d.set_relabel_callback(delivered.extend)

        raced = {}
        original = d._recluster

        def racing_recluster():
            ops = original()
            assert ops, "fixture must produce relabel ops"
            # The human correction lands here: after the diarizer released the
            # lock, before the ops reach the store.
            raced["segment_id"] = ops[0]["segment_id"]
            raced["stale_pid"] = ops[0]["participant_id"]
            d.pin(ops[0]["segment_id"], "p_human")
            return ops

        d._recluster = racing_recluster
        d._new_since_recluster = 25
        d.assign(_segment("sg_new", 99.0), np.zeros(16000, dtype=np.float32),
                 16000)

        segment_id = raced["segment_id"]
        assert raced["stale_pid"] != "p_human"
        # The stale op never reaches the store...
        assert all(op["segment_id"] != segment_id for op in delivered)
        # ...and the in-memory record keeps the human's choice, still pinned,
        # so the DB and the diarizer cannot diverge.
        record = d._by_segment_id[segment_id]
        assert record.participant_id == "p_human"
        assert record.pinned is True

    def test_filter_keeps_ops_that_are_still_valid(self):
        d = self._racing_diarizer()
        ops = d._recluster()
        assert ops
        assert d._filter_stale_ops(ops) == ops

    def test_filter_drops_ops_for_unknown_segments(self):
        d = self._racing_diarizer()
        ops = [{"op": "reassign_segment_speaker", "segment_id": "sg_missing",
                "participant_id": "p_A"}]
        assert d._filter_stale_ops(ops) == []


class TestPinOnUnknownSegment:
    def test_pin_never_embedded_segment_does_not_raise(self):
        d = _make_diarizer()
        d.pin("sg_never_seen", "p_X")  # mic channel / pre-diarizer / too short
        record = d._by_segment_id["sg_never_seen"]
        assert record.pinned is True
        assert record.participant_id == "p_X"
        assert record.embedding is None

    def test_pin_on_unknown_segment_is_not_a_temporal_donor(self):
        d = _make_diarizer()
        d.pin("sg_never_seen", "p_X")
        with d._lock:
            # -inf placeholder times keep it out of the inheritance window.
            assert d._nearest_temporal_pid(_segment("sg_q", 0.0)) is None

    def test_pin_before_embedding_survives_later_assignment(self):
        vectors = _synthetic_speakers(4, speakers=2)
        d = _make_diarizer(vectors)
        d.pin("sg_0", "p_human")
        pid = d.assign(_segment("sg_0", 0.0),
                       np.zeros(16000, dtype=np.float32), 16000)
        assert pid == "p_human"
        record = d._by_segment_id["sg_0"]
        assert record.pinned is True
        assert record.embedding is not None
        assert d._store.n == 0  # no phantom "Speaker N" participant created

    def test_pin_moves_weight_between_clusters(self):
        vectors = [_unit([1, 0, 0]), _unit([0, 1, 0])]
        d = _make_diarizer(vectors)
        first = d.assign(_segment("sg_0", 0.0),
                         np.zeros(16000, dtype=np.float32), 16000)
        second = d.assign(_segment("sg_1", 2.0),
                          np.zeros(16000, dtype=np.float32), 16000)
        assert first != second
        d.pin("sg_0", second)
        target = d._find_cluster(second)
        assert "sg_0" in target.pinned_segments
        assert d._find_cluster(first) is None  # emptied cluster is dropped


class TestGracefulDegradation:
    def test_runtime_failure_degrades_once_and_never_blocks_asr(self, caplog):
        embedder = RaisingEmbedder()
        d = _make_diarizer(embedder=embedder)
        audio = np.zeros(16000, dtype=np.float32)

        with caplog.at_level(logging.WARNING,
                             logger="meeting.diarize.clustering"):
            started = time.perf_counter()
            results = [d.assign(_segment(f"sg_{i}", float(i)), audio, 16000)
                       for i in range(50)]
            elapsed = time.perf_counter() - started

        assert results == [None] * 50
        assert d.is_available() is False
        # Degraded mode short-circuits: the embedder is called once, not 50x,
        # so the ASR worker is never blocked by a broken diarizer.
        assert embedder.calls == 1
        assert elapsed < 1.0
        failures = [r for r in caplog.records
                    if r.name == "meeting.diarize.clustering"
                    and r.levelno >= logging.WARNING]
        assert len(failures) == 1

    def test_is_available_latches_false_when_embedder_reports_unavailable(self):
        d = _make_diarizer([_unit([1, 0, 0])])
        assert d.is_available() is True
        d._embedder.available = False
        assert d.is_available() is False
        d._embedder.available = True  # recovery must not un-degrade
        assert d.is_available() is False

    def test_is_available_false_when_availability_probe_raises(self):
        class Exploding:
            @property
            def available(self):
                raise RuntimeError("boom")

        d = _make_diarizer(embedder=Exploding())
        assert d.is_available() is False

    def test_degraded_diarizer_still_satisfies_protocol(self):
        from meeting.interfaces import Diarizer
        d = _make_diarizer(embedder=RaisingEmbedder())
        assert isinstance(d, Diarizer)
        d.assign(_segment("sg_0", 0.0), np.zeros(16000, dtype=np.float32),
                 16000)
        assert d.is_available() is False
        d.pin("sg_0", "p_X")  # still accepts corrections without raising


class TestBoundedRecluster:
    def test_large_meeting_recluster_stays_fast(self):
        vectors = _synthetic_speakers(3000)
        d = _make_diarizer([vectors[0]])
        _prefill(d, vectors)
        d.set_relabel_callback(lambda ops: None)
        d._new_since_recluster = 25

        started = time.perf_counter()
        d.assign(_segment("sg_new", 5000.0),
                 np.zeros(16000, dtype=np.float32), 16000)
        elapsed = time.perf_counter() - started

        # Average-linkage over an unbounded set costs ~33s at N=3000. The
        # bound makes a pass cost tens of milliseconds; the generous ceiling
        # here fails loudly if the O(N^3) behavior ever returns.
        assert elapsed < 2.0

    def test_working_set_is_bounded_and_keeps_every_pin(self):
        vectors = _synthetic_speakers(2000)
        d = _make_diarizer()
        records = _prefill(d, vectors)
        for record in records[:20]:  # old pins, far outside the recent window
            record.pinned = True

        with d._lock:
            selected, anchors = d._working_set(records)

        assert len(selected) <= MAX_WORKING_SET
        assert len([r for r in selected if not r.pinned]) <= RECLUSTER_WINDOW
        selected_ids = {r.segment_id for r in selected}
        assert all(r.segment_id in selected_ids for r in records if r.pinned)
        # Out-of-window history still votes, via one anchor per participant.
        assert {a.participant_id for a in anchors} == {"p_0", "p_1", "p_2", "p_3"}
        assert all(a.weight < PIN_WEIGHT for a in anchors)

    def test_pinned_segments_keep_assignment_across_large_recluster(self):
        vectors = _synthetic_speakers(1200)
        d = _make_diarizer()
        records = _prefill(d, vectors)
        # Deliberately "wrong" pins: the embeddings look like p_0 but a human
        # said p_pinned. Both an old and a recent one must be untouched.
        for index in (4, 1100):
            records[index].pinned = True
            records[index].participant_id = "p_pinned"

        ops = d._recluster()

        for index in (4, 1100):
            assert d._by_segment_id[f"sg_{index}"].participant_id == "p_pinned"
            assert d._by_segment_id[f"sg_{index}"].pinned is True
        assert all(op["segment_id"] not in ("sg_4", "sg_1100") for op in ops)

    def test_cluster_quality_preserved_on_four_speaker_fixture(self):
        vectors = _synthetic_speakers(1000)
        d = _make_diarizer()
        _prefill(d, vectors)

        d._recluster()

        # Every speaker keeps exactly one participant, and no two speakers
        # share one: the bounded pass must not smear identities together.
        by_speaker = {}
        for i, record in enumerate(d._records):
            by_speaker.setdefault(i % 4, set()).add(record.participant_id)
        assert all(len(pids) == 1 for pids in by_speaker.values())
        assert len({next(iter(p)) for p in by_speaker.values()}) == 4

    def test_recent_window_is_reclustered_not_the_whole_history(self):
        vectors = _synthetic_speakers(1000)
        d = _make_diarizer()
        records = _prefill(d, vectors)
        stale_old, stale_recent = records[0], records[-1]
        stale_old.participant_id = "p_wrong"
        stale_recent.participant_id = "p_wrong"

        ops = d._recluster()
        moved = {op["segment_id"] for op in ops}

        assert stale_recent.segment_id in moved  # inside the window: fixed
        assert stale_old.segment_id not in moved  # outside: left alone


class TestOverMergeSplit:
    def test_recluster_splits_an_over_merged_participant(self):
        vectors = _synthetic_speakers(40, speakers=2)
        d = _make_diarizer()
        records = _prefill(d, vectors, pid_of=lambda i: "p_A")

        ops = d._recluster()

        assert d._store.n == 1  # exactly one new participant created
        new_pid = "p_speaker_1"
        first = {r.participant_id for i, r in enumerate(records) if i % 2 == 0}
        second = {r.participant_id for i, r in enumerate(records) if i % 2 == 1}
        assert len(first) == 1 and len(second) == 1
        assert first != second
        assert {"p_A", new_pid} == first | second
        assert len(ops) == 20
        assert all(op["participant_id"] == new_pid for op in ops)

    def test_repeated_reclusters_converge(self):
        # Three voices merged into one participant: one pass separates them,
        # and every later pass is a no-op (no runaway "Speaker N" creation).
        vectors = _synthetic_speakers(60, speakers=3)
        d = _make_diarizer()
        _prefill(d, vectors, pid_of=lambda i: "p_A")

        assert d._recluster()
        assert d._store.n == 2
        for _ in range(5):
            assert d._recluster() == []
        assert d._store.n == 2
        assert len(d._clusters) == 3

    def test_split_participant_is_created_outside_the_diarizer_lock(self):
        vectors = _synthetic_speakers(40, speakers=2)
        d = _make_diarizer()
        _prefill(d, vectors, pid_of=lambda i: "p_A")

        observed = []

        class LockProbingStore(FakeStore):
            def apply(self, actor_type, actor_id, ops):
                observed.append(d._lock.acquire(blocking=False))
                if observed[-1]:
                    d._lock.release()
                return super().apply(actor_type, actor_id, ops)

        d._store = LockProbingStore()
        d._recluster()
        assert observed and all(observed), "store.apply ran under the diarizer lock"

    def test_tiny_group_does_not_spawn_a_phantom_speaker(self):
        # One stray vector from a second voice: below MIN_SPLIT_SEGMENTS, so
        # re-clustering must not invent a participant for it.
        vectors = _synthetic_speakers(20, speakers=1)
        stray = _synthetic_speakers(2, speakers=1, seed=99)
        d = _make_diarizer()
        _prefill(d, vectors + stray, pid_of=lambda i: "p_A")

        assert MIN_SPLIT_SEGMENTS > 2
        d._recluster()
        assert d._store.n == 0

    def test_split_failure_leaves_the_group_merged(self):
        vectors = _synthetic_speakers(40, speakers=2)
        d = _make_diarizer()
        records = _prefill(d, vectors, pid_of=lambda i: "p_A")

        class RejectingStore:
            def apply(self, actor_type, actor_id, ops):
                return [OpResult(ok=False, op=ops[0], reason="rejected")]

        d._store = RejectingStore()
        ops = d._recluster()  # must not raise or degrade the diarizer

        assert ops == []
        assert {r.participant_id for r in records} == {"p_A"}


class TestAverageLinkageScaling:
    def test_linkage_matches_reference_on_small_input(self):
        vectors = np.stack(_synthetic_speakers(24, speakers=3))
        dist = np.clip(1.0 - vectors @ vectors.T, 0.0, 2.0)
        groups = _average_linkage(dist, threshold=0.38)
        assert len(groups) == 3
        for group in groups:
            assert len({i % 3 for i in group}) == 1

    def test_singleton_and_empty_inputs(self):
        assert _average_linkage(np.zeros((0, 0)), 0.38) == []
        assert _average_linkage(np.zeros((1, 1)), 0.38) == [[0]]


class TestFbank:
    def _tone(self, freq, seconds=1.0):
        t = np.arange(int(fbank_mod.SAMPLE_RATE * seconds), dtype=np.float64)
        return (0.5 * np.sin(2 * np.pi * freq * t / fbank_mod.SAMPLE_RATE)
                ).astype(np.float32)

    def test_shape_and_determinism(self):
        audio = self._tone(440.0, seconds=0.5)
        feats = fbank_mod.compute_fbank(audio)
        expected_frames = (
            (audio.size - fbank_mod.FRAME_LENGTH) // fbank_mod.FRAME_SHIFT + 1)
        assert feats.shape == (expected_frames, fbank_mod.NUM_MEL_BINS)
        assert feats.dtype == np.float32
        assert np.isfinite(feats).all()
        assert np.array_equal(feats, fbank_mod.compute_fbank(audio))

    def test_short_audio_yields_no_frames(self):
        feats = fbank_mod.compute_fbank(np.zeros(100, dtype=np.float32))
        assert feats.shape == (0, fbank_mod.NUM_MEL_BINS)

    def test_int16_input_matches_normalized_float(self):
        audio = self._tone(300.0, seconds=0.2)
        as_int16 = (audio * 32768.0).astype(np.int16)
        np.testing.assert_allclose(
            fbank_mod.compute_fbank(as_int16),
            fbank_mod.compute_fbank(as_int16.astype(np.float32) / 32768.0),
            rtol=1e-4, atol=1e-4,
        )

    def test_cmn_zeroes_the_time_mean(self):
        feats = fbank_mod.compute_fbank(self._tone(500.0, seconds=0.3))
        normalized = fbank_mod.apply_cmn(feats)
        np.testing.assert_allclose(normalized.mean(axis=0), 0.0, atol=1e-3)
        assert fbank_mod.apply_cmn(
            np.zeros((0, fbank_mod.NUM_MEL_BINS), np.float32)).shape[0] == 0

    def test_filterbank_reaches_nyquist(self):
        # Kaldi / torchaudio / WeSpeaker default: high_freq = Nyquist. A
        # narrower band (the old 7600 Hz) shifts every mel bin away from the
        # model's training features.
        assert fbank_mod.HIGH_FREQ == fbank_mod.SAMPLE_RATE / 2.0
        filters = fbank_mod._build_mel_filters()
        n_bins = fbank_mod.N_FFT // 2 + 1
        assert filters.shape == (fbank_mod.NUM_MEL_BINS, n_bins)

        freqs = np.linspace(0.0, fbank_mod.SAMPLE_RATE / 2.0, n_bins)
        coverage = filters.sum(axis=0)
        assert coverage[freqs > 7600.0].max() > 0.0
        # The Nyquist bin sits exactly on the last triangle's right edge.
        assert coverage[-1] == pytest.approx(0.0, abs=1e-6)
        assert coverage[-2] > 0.0
        assert filters.min() >= 0.0

    def test_top_mel_bin_responds_to_a_7800_hz_tone(self):
        # Functional companion to the band-edge guard above (which is the
        # actual 7600 Hz regression detector): near-Nyquist energy must land
        # in the top mel bin rather than being discarded.
        top_tone = fbank_mod.compute_fbank(self._tone(7800.0, seconds=0.2))
        silence = fbank_mod.compute_fbank(
            np.zeros(fbank_mod.SAMPLE_RATE // 5, dtype=np.float32))
        assert top_tone[:, -1].mean() > silence[:, -1].mean() + 10.0


#: Set both to run the embedding-parity gate from the plan
#: ("cosine >= 0.999 vs reference vectors"). ``..._REF`` is an ``.npz`` with
#: ``audio_<i>`` (float32 mono 16 kHz) and ``embedding_<i>`` (reference
#: embedding) array pairs produced by the reference WeSpeaker pipeline.
_PARITY_MODEL = os.environ.get("OPENWHISPER_SPEAKER_MODEL")
_PARITY_REF = os.environ.get("OPENWHISPER_SPEAKER_PARITY_REF")


@pytest.mark.skipif(
    not (_PARITY_MODEL and _PARITY_REF),
    reason="set OPENWHISPER_SPEAKER_MODEL and OPENWHISPER_SPEAKER_PARITY_REF"
           " to run the embedding parity gate",
)
def test_embedding_parity_against_reference_vectors():
    """Gate from the plan: our fbank+ONNX path must match the reference.

    Guards the whole feature front end (window, pre-emphasis, mel band edges,
    CMN) against silent drift; a mismatch here degrades every embedding and
    therefore all clustering quality.
    """
    from meeting.diarize.embedder import SpeakerEmbedder

    data = np.load(_PARITY_REF)
    pairs = sorted(k for k in data.files if k.startswith("audio_"))
    assert pairs, f"{_PARITY_REF} contains no audio_<i> arrays"

    embedder = SpeakerEmbedder(_PARITY_MODEL)
    for key in pairs:
        index = key.split("_", 1)[1]
        reference = np.asarray(data[f"embedding_{index}"], dtype=np.float32)
        reference = reference / np.linalg.norm(reference)
        produced = embedder.embed(np.asarray(data[key], dtype=np.float32))
        assert produced is not None, f"no embedding for {key}"
        assert float(np.dot(produced, reference)) >= 0.999, key
