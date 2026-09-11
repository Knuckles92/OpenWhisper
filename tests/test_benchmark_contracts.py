"""Executable validity contracts for benchmark output, without model calls."""
from types import SimpleNamespace
import json
from unittest.mock import Mock

import pytest

from benchmarks.meeting_mode import run as ami_run
from benchmarks.meeting_mode.ami import ReferenceWord
from benchmarks.meeting_mode.metrics import score_text
from benchmarks.meeting_mode import product_eval as product


@pytest.mark.parametrize("module_name", ["accuracy_benchmark", "model_benchmark"])
@pytest.mark.parametrize("hypothesis", ["three two one", "one two three invented claim", "one", ""])
def test_accuracy_penalizes_order_insertions_and_omissions(hypothesis, module_name):
    import importlib
    calculate_word_accuracy = importlib.import_module("benchmarks." + module_name).calculate_word_accuracy
    assert calculate_word_accuracy("one two three", hypothesis) < 100


def test_silence_preserves_insertions_and_has_no_defined_wer():
    score = score_text("", "invented words")
    assert score["insertions"] == score["errors"] == 2
    assert score["words"] == 0 and score["wer"] is None


@pytest.fixture
def scored_meeting(monkeypatch, tmp_path):
    monkeypatch.setattr(ami_run, "parse_reference_words", lambda *_: [
        ReferenceWord("hello", 0, 1, "A"), ReferenceWord("world", 1, 2, "A"),
    ])
    def make(offline, enabled=True):
        segment = dict(start_s=0, end_s=2, text="hello world")
        return ami_run._score_result(dict(
            meeting_id="synthetic", duration_s=3600, elapsed_s=1, chunks=1, rtf=1/3600,
            draft_segments=[segment], final_segments=[segment],
            offline_segments=offline, run_offline=enabled,
        ), tmp_path)
    return make


def test_empty_offline_is_scored_as_deletions_and_fails_gate(scored_meeting):
    result = scored_meeting([])
    assert result["offline_score"]["deletions"] == 2
    summary = ami_run._summary([result]*10, "fake", "en", False, 5, 20, 50, True)
    assert summary["quality_gate"]["product"] == "offline"
    assert not summary["quality_gate"]["passed"]
    assert summary["offline"]["wer"] == 1


def test_disabled_offline_does_not_use_cached_offline_score(scored_meeting):
    result = scored_meeting([], enabled=False)
    assert result["offline_score"] is None
    summary = ami_run._summary([result]*10, "fake", "en", False, 5, 20, 50, False)
    assert summary["quality_gate"]["passed"]


def test_missing_required_offline_score_cannot_substitute_draft(scored_meeting):
    result = scored_meeting([])
    result["offline_score"] = None
    with pytest.raises(ValueError, match="offline"):
        ami_run._summary([result], "fake", "en", False, 5, 20, 50, True)


def test_partial_polish_failure_is_not_completed():
    from meeting.interfaces import AgentResult
    host = product.ProductEvalHost("m", [
        dict(id=f"sg_{i}", start_s=i, end_s=i+1, text="word") for i in range(401)
    ])
    agent = SimpleNamespace(checkpoint=Mock(side_effect=[
        AgentResult(ok=True), AgentResult(ok=False, error="second block failed"),
    ]))
    result, _ = product._run_polish(agent, host)
    assert result.status == "failed"
    assert "second block failed" in result.message


@pytest.mark.parametrize("content", ["", "{", "[]", "{}", '{"winner":"tie"}'])
def test_invalid_judge_is_unjudged(monkeypatch, content):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    )))
    monkeypatch.setattr(product, "_judge_client", lambda *_: client)
    result = product.judge_packages(meeting_id="m", description="", reference="hello",
        legacy={}, clean={}, provider="fake", model="fake", api_key="fake")
    assert result["winner"] == "unjudged"
    assert result["parse_error"]


def test_valid_judge_tie_remains_a_tie():
    fields = ("transcript_usefulness", "topic_accuracy", "key_points_fidelity",
              "decisions_actions_precision", "notes_and_timeline_quality", "overall_record")
    result = product._normalize_judgment({
        "legacy": dict.fromkeys(fields, 3), "clean": dict.fromkeys(fields, 3), "winner": "tie",
    })
    assert result["winner"] == "tie"
    assert not result.get("parse_error")


@pytest.mark.parametrize("failure", [False, True])
def test_corpus_counts_silence_and_propagates_failures(monkeypatch, tmp_path, failure):
    import numpy as np
    from scripts import benchmark_local_asr_corpus as corpus
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *a, **k: np.zeros(16000))
    backend = SimpleNamespace(device="cpu", device_info="fake", model_name="base",
        is_available=lambda: True, transcribe=Mock(side_effect=RuntimeError("decode failed")) if failure else
        Mock(return_value="invented words"), cleanup=Mock())
    monkeypatch.setattr("transcriber.local_backend.LocalWhisperBackend", lambda *a, **k: backend)
    monkeypatch.setattr(corpus, "model_identity", lambda *_: {"fake": True})
    manifest = tmp_path/"manifest.json"
    (tmp_path/"silence.wav").write_bytes(b"synthetic")
    manifest.write_text(json.dumps({"clips":[{"audio_path":"silence.wav","reference":"","group":"silence"}]}))
    output = tmp_path/"result.json"
    code = corpus.run(SimpleNamespace(manifest=manifest, models="base", cpu_only=True,
        test_root=None, output=output))
    row = json.loads(output.read_text())["results"][0]
    assert code == int(failure)
    backend.cleanup.assert_called_once()
    if failure:
        assert "decode failed" in row["error"]
    else:
        assert row["groups"]["silence"]["insertions"] == 2
        assert row["groups"]["silence"]["errors"] == 2
        assert row["groups"]["silence"]["normalized_wer"] is None


def test_provenance_rejects_legacy_changed_audio_settings_and_dirty_source(monkeypatch, tmp_path):
    from benchmarks import provenance
    monkeypatch.setattr(provenance, "ROOT", tmp_path)
    (tmp_path/"config.py").write_text("one")
    audio = tmp_path/"audio.wav"
    audio.write_bytes(b"one")
    first = provenance.identity(inputs=[audio], settings={"model":"base"})
    assert not provenance.reusable({}, first)
    assert provenance.reusable({"provenance": first}, first)
    assert not provenance.reusable({"provenance":first, "error":"failed"}, first)
    for change in ("audio", "settings", "source"):
        audio.write_bytes(b"two" if change == "audio" else b"one")
        (tmp_path/"config.py").write_text("two" if change == "source" else "one")
        stamp = provenance.identity(inputs=[audio], settings={"model":"tiny" if change=="settings" else "base"})
        assert not provenance.reusable({"provenance":first}, stamp)


@pytest.mark.parametrize("field", ["artifacts", "model_snapshot", "runtime_manifest", "device_info"])
def test_preview_pool_rejects_different_installed_identity(tmp_path, field):
    from copy import deepcopy
    from scripts.report_live_preview import summarize
    row = dict(model="base", actual_device="cpu",
               artifacts={"revision":"a"}, model_snapshot="a", runtime_manifest={"version":1},
               device_info="cpu", profiles=[dict(mode="window", clips=[], summary={}, effective_cadence_s=1)])
    report = dict.fromkeys(("manifest_sha256","chunk_s","overlap_s","native_cadence_s",
                           "normalization","decoding","corpus","source_sha256"), "same")
    report["results"] = [row]
    other = deepcopy(report)
    other["results"][0][field] = "different"
    paths = [tmp_path/"first.json", tmp_path/"second.json"]
    for path, value in zip(paths, (report, other)):
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match=field):
        summarize(paths)


def test_production_replay_waits_for_arrivals_runs_first_notes_and_keeps_corrections(monkeypatch):
    from meeting.interfaces import AgentResult
    from meeting.agent import openrouter_direct
    calls = []
    class Agent:
        supports_notes_pass = True
        def initialize(self, cfg, host): self.host = host
        def is_healthy(self): return True
        def shutdown(self): pass
        def checkpoint(self, payload):
            now = self.host.clock.now_s()
            assert all(seg["end_s"] <= now for seg in self.host.get_transcript())
            calls.append((now, payload.is_notes, payload.is_polish, [s["id"] for s in payload.new_segments]))
            if not payload.is_notes and not payload.is_polish:
                results = self.host.apply_agent_ops([dict(op="revise_segment_text", segment_id="sg_first",
                    text="Maia", evidence=["sg_first"])])
                assert results[0].ok
            return AgentResult(ok=True)
    monkeypatch.setattr(product, "create_agent_core", lambda _: Agent())
    before_timeout = openrouter_direct._CHECKPOINT_TIMEOUT_S
    source = [dict(id="sg_first",start_s=0,end_s=5,text="Maya"),
              dict(id="sg_future",start_s=140,end_s=145,text="Later")]
    result = product.simulate_live_meeting("m", source, provider="fake", model="fake", api_key="fake")
    assert calls[0][0] == 120
    assert calls[0][3] == ["sg_first"]
    assert any(now == 120 and notes for now, notes, _, _ in calls)
    assert result["segments"][0]["text"] == "Maia"
    assert source[0]["text"] == "Maya"
    assert openrouter_direct._CHECKPOINT_TIMEOUT_S == before_timeout


@pytest.mark.parametrize("winner,failed_stage", [("tie",False),("unjudged",False),("tie",True)])
def test_product_command_carries_corrected_live_text_and_returns_failure(monkeypatch, tmp_path, winner, failed_stage):
    source = tmp_path/"m.json"
    draft = [dict(id="sg_1",start_s=0,end_s=2,text="Maya",channel="loopback")]
    corrected = [dict(draft[0],text="Maia")]
    source.write_text(json.dumps(dict(draft_segments=draft, offline_segments=corrected, run_offline=True)))
    monkeypatch.setattr(product, "_parse_args", lambda _: SimpleNamespace(
        results_dir=tmp_path, data_dir=tmp_path, out_dir=tmp_path/"out", meetings="m",
        no_live=False, live_window_s=1, polish_timeout=1, consolidation_timeout=1))
    monkeypatch.setattr(product, "find_provider_api_key", lambda _: "synthetic")
    monkeypatch.setattr(product, "select_meetings", lambda _: [SimpleNamespace(meeting_id="m",description="synthetic")])
    monkeypatch.setattr(product, "annotation_root", lambda _: tmp_path)
    monkeypatch.setattr(product, "parse_reference_words", lambda *_: [])
    monkeypatch.setattr(product, "simulate_live_meeting", lambda *a,**k: dict(state={},segments=corrected,stats={}))
    calls=[]
    def pipeline(meeting, segments, **kwargs):
        calls.append((segments,kwargs))
        return dict(consolidation={"status":"failed" if failed_stage else "completed"},polish=None)
    monkeypatch.setattr(product, "run_product_pipeline", pipeline)
    monkeypatch.setattr(product, "judge_packages", lambda **_: {"winner":winner})
    assert product.main([]) == int(winner=="unjudged" or failed_stage)
    assert calls[0][0][0]["text"] == "Maia"
    assert calls[1][1]["old_segments"][0]["text"] == "Maia"
    summary=json.loads((tmp_path/"out"/"summary.json").read_text())
    assert summary["wins"][winner] == 1
    assert summary["wins"]["tie"] == int(winner=="tie")


@pytest.mark.parametrize("module_name", ["accuracy_benchmark", "model_benchmark"])
@pytest.mark.parametrize("outcome", ["success","row_failure","missing","exception"])
def test_legacy_command_exits_reflect_missing_and_failed_work(monkeypatch, module_name, outcome):
    import importlib
    module = importlib.import_module("benchmarks."+module_name)
    result = SimpleNamespace(success=outcome!="row_failure")
    instance = SimpleNamespace(
        backends={"fake":Mock()}, sample_keys=["sample"], initialization_failed=False,
        local_models_to_test=[], results=[] if outcome=="missing" else [result],
        run_benchmark=Mock(side_effect=RuntimeError("synthetic failure") if outcome=="exception" else None),
        cleanup=Mock())
    monkeypatch.setattr(module, "AccuracyBenchmark" if module_name=="accuracy_benchmark" else "ModelBenchmark", lambda **_:instance)
    args = ["--skip-api"] if module_name=="accuracy_benchmark" else ["--skip-api","--durations","1"]
    assert module.main(args) == int(outcome!="success")
    instance.cleanup.assert_called_once()
