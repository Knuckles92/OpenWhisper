"""Benchmark current End (redecode + polish) vs Gemini-on-full-meeting-audio.

Uses real local Meeting Mode recordings. The production SQLite database is
read-only; every write lands under the ignored results directory.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app_qt as _app_bootstrap  # noqa: E402,F401

from benchmarks.meeting_mode.metrics import edit_counts, normalize_tokens  # noqa: E402
from meeting.asr.offline import transcribe_meeting_sessions  # noqa: E402
from meeting.export.transcript_txt import format_clock  # noqa: E402
from services.transcript_cleanup import find_api_key  # noqa: E402

# Three real ended meetings that still have a continuous session WAV.
# Short / short / medium so the Gemini upload stays inside request limits
# after MP3 compression, while still covering different topics.
DEFAULT_MEETING_IDS = (
    "m_55ab44b05fd7",  # ~3.2 min — Framework laptop design
    "m_a8764d0d6ad5",  # ~2.7 min — voice dictation as planning
    "m_4851d8ae3556",  # ~12.6 min — dispatching unedited prompts
)

GEMINI_AUDIO_PROMPT = """\
Transcribe this entire meeting recording from start to finish.

Return JSON only, no markdown fences:
{
  "segments": [
    {"start_s": 0.0, "end_s": 5.2, "speaker": "A", "text": "Spoken sentence."}
  ]
}

Rules:
- Transcribe all audible speech. Do not summarize or skip stretches.
- Use ordinary punctuation and capitalization.
- Drop filler-only um/uh. Keep every lexical word that was spoken.
- Do not invent names, numbers, or claims that are not in the audio.
- start_s / end_s are seconds from the start of this file.
- Label distinct voices A, B, C consistently. Use "Me" only if one side is clearly a close microphone.
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg is required to compress meeting audio for Gemini")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _load_meeting(conn: sqlite3.Connection, meeting_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM meeting_sessions WHERE id = ?", (meeting_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown meeting: {meeting_id}")
    meeting = dict(row)
    meeting["chunks"] = [
        dict(chunk)
        for chunk in conn.execute(
            "SELECT * FROM meeting_audio_chunks WHERE meeting_id = ? ORDER BY start_s",
            (meeting_id,),
        )
    ]
    meeting["stored_segments"] = [
        dict(seg)
        for seg in conn.execute(
            "SELECT * FROM meeting_segments WHERE meeting_id = ? ORDER BY start_s",
            (meeting_id,),
        )
    ]
    return meeting


def _session_wav(meeting: dict[str, Any], root: Path) -> Path:
    spool = Path(meeting["spool_dir"])
    if not spool.is_absolute():
        spool = root / spool
    for name in ("playback.wav", "loopback_session.wav"):
        candidate = spool / name
        if candidate.is_file() and candidate.stat().st_size > 1000:
            return candidate
    raise FileNotFoundError(f"No session WAV in {spool}")


def _wav_duration_s(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return frames / float(rate or 1)


def _compress_mp3(src: Path, dest: Path) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    subprocess.run(
        [
            _ffmpeg_bin(),
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return {
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "elapsed_s": time.perf_counter() - started,
        "source_bytes": src.stat().st_size,
        "source_path": str(src),
    }


def _segment_dict(seg: Any) -> dict[str, Any]:
    if isinstance(seg, dict):
        return {
            "id": seg.get("id") or seg.get("segment_id"),
            "start_s": float(seg.get("start_s") or 0.0),
            "end_s": float(seg.get("end_s") or 0.0),
            "text": (seg.get("text") or "").strip(),
            "channel": seg.get("channel") or "",
            "speaker": seg.get("speaker")
            or seg.get("speaker_participant_id")
            or "",
        }
    return {
        "id": seg.segment_id,
        "start_s": float(seg.start_s),
        "end_s": float(seg.end_s),
        "text": (seg.text or "").strip(),
        "channel": seg.channel,
        "speaker": seg.speaker_participant_id or "",
    }


def _render_transcript(
    title: str,
    segments: list[dict[str, Any]],
    *,
    extra_header: str = "",
) -> str:
    lines = [title]
    if extra_header:
        lines.append(extra_header)
    lines.append("")
    body = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        stamp = format_clock(float(seg.get("start_s") or 0.0))
        speaker = (seg.get("speaker") or seg.get("channel") or "Speaker").strip()
        if speaker == "loopback":
            speaker = "Others"
        elif speaker == "mic":
            speaker = "Me"
        body.append(f"[{stamp}] {speaker}: {text}")
    lines.append("\n".join(body) if body else "(empty transcript)")
    lines.append("")
    return "\n".join(lines)


def _plain_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(
        (seg.get("text") or "").strip()
        for seg in segments
        if (seg.get("text") or "").strip()
    )


def _compare_texts(reference: str, hypothesis: str) -> dict[str, Any]:
    ref = normalize_tokens(reference)
    hyp = normalize_tokens(hypothesis)
    counts = edit_counts(ref, hyp)
    denom = max(1, len(ref))
    return {
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "substitutions": counts.substitutions,
        "deletions": counts.deletions,
        "insertions": counts.insertions,
        "wer": counts.errors / denom,
        "substitution_rate": counts.substitutions / denom,
        "deletion_rate": counts.deletions / denom,
        "insertion_rate": counts.insertions / denom,
    }


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        raw = usage.model_dump()
    elif isinstance(usage, dict):
        raw = usage
    else:
        raw = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cost": getattr(usage, "cost", None),
        }
    return {key: value for key, value in raw.items() if value is not None}


def _run_redecode(
    meeting: dict[str, Any],
    *,
    root: Path,
    model_name: str,
    language: str,
    backend: Any,
) -> dict[str, Any]:
    spool = Path(meeting["spool_dir"])
    if not spool.is_absolute():
        spool = root / spool
    started = time.perf_counter()
    decoded = transcribe_meeting_sessions(
        backend.model,
        str(spool),
        meeting["id"],
        meeting["chunks"],
        language=language,
    )
    elapsed = time.perf_counter() - started
    segments = [_segment_dict(seg) for seg in decoded]
    audio_s = float(meeting.get("audio_s") or 0.0)
    return {
        "ok": bool(segments),
        "elapsed_s": elapsed,
        "rtf": elapsed / audio_s if audio_s else 0.0,
        "model": model_name,
        "language": language,
        "segments": segments,
        "word_count": len(normalize_tokens(_plain_text(segments))),
    }


def _run_polish(
    meeting_id: str,
    segments: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    from benchmarks.meeting_mode.product_eval import ProductEvalHost, _run_polish
    from meeting.agent.base import create_agent_core
    from meeting.agent.prompts import build_system_prompt
    from meeting.interfaces import AgentConfig

    started = time.perf_counter()
    host = ProductEvalHost(meeting_id, segments)
    agent = create_agent_core("direct")
    agent.initialize(
        AgentConfig(
            meeting_id=meeting_id,
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=build_system_prompt(),
        ),
        host,
    )
    if not agent.is_healthy():
        raise RuntimeError("Meeting polish agent is offline")
    host.allow_agent_writes()
    try:
        outcome, op_stats = _run_polish(agent, host)
        polished = [_segment_dict(seg) for seg in host.get_transcript()]
    finally:
        host.revoke_agent_writes()
        agent.shutdown()
    elapsed = time.perf_counter() - started
    return {
        "ok": getattr(outcome, "status", "") == "completed",
        "elapsed_s": elapsed,
        "message": getattr(outcome, "message", ""),
        "op_stats": op_stats,
        "segments": polished,
        "word_count": len(normalize_tokens(_plain_text(polished))),
        "provider": provider,
        "model": model,
    }


def _parse_gemini_transcript(text: str) -> list[dict[str, Any]]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return [{
                "id": "gemini_000",
                "start_s": 0.0,
                "end_s": 0.0,
                "text": raw,
                "channel": "gemini",
                "speaker": "Gemini",
            }]
        payload = json.loads(raw[start : end + 1])
    rows = payload.get("segments") if isinstance(payload, dict) else payload
    segments = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        text_value = (row.get("text") or "").strip()
        if not text_value:
            continue
        segments.append({
            "id": f"gemini_{index:04d}",
            "start_s": float(row.get("start_s") or 0.0),
            "end_s": float(row.get("end_s") or 0.0),
            "text": text_value,
            "channel": "gemini",
            "speaker": (row.get("speaker") or "Speaker").strip(),
        })
    return segments


def _run_gemini_audio(
    mp3_path: Path,
    *,
    model: str,
    api_key: str,
    timeout_s: float,
) -> dict[str, Any]:
    from openai import OpenAI

    audio_b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "OpenWhisper gemini-audio benchmark"},
        timeout=timeout_s,
    )
    started = time.perf_counter()
    last_error = ""
    response = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": GEMINI_AUDIO_PROMPT},
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": audio_b64,
                                    "format": "mp3",
                                },
                            },
                        ],
                    }
                ],
                max_tokens=32768,
                temperature=0,
            )
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(2.0)
                continue
            elapsed = time.perf_counter() - started
            return {
                "ok": False,
                "elapsed_s": elapsed,
                "model": model,
                "finish_reason": "exception",
                "error": last_error,
                "usage": {},
                "raw_text": "",
                "segments": [],
                "word_count": 0,
                "audio_bytes": mp3_path.stat().st_size,
                "request_id": None,
            }
        choice = (response.choices or [None])[0]
        text = ""
        finish = ""
        if choice is not None:
            message = choice.message
            text = (message.content or "") if message else ""
            finish = choice.finish_reason or ""
            if not text and message is not None:
                refusal = getattr(message, "refusal", None)
                if refusal:
                    last_error = str(refusal)
        extra = getattr(response, "error", None)
        if extra:
            last_error = str(extra)
        if text.strip() and finish not in {"error", "content_filter"}:
            break
        last_error = last_error or f"empty Gemini response (finish={finish or 'unknown'})"
        if attempt == 0:
            time.sleep(2.0)
    elapsed = time.perf_counter() - started
    usage = _usage_dict(getattr(response, "usage", None) if response else None)
    segments = _parse_gemini_transcript(text)
    return {
        "ok": bool(segments) and finish not in {"length", "content_filter", "error"},
        "elapsed_s": elapsed,
        "model": model,
        "finish_reason": finish,
        "error": last_error,
        "usage": usage,
        "raw_text": text,
        "segments": segments,
        "word_count": len(normalize_tokens(_plain_text(segments))),
        "audio_bytes": mp3_path.stat().st_size,
        "request_id": getattr(response, "id", None) if response else None,
    }


def _write_meeting_outputs(
    meeting_dir: Path,
    title: str,
    stored: list[dict[str, Any]],
    current: dict[str, Any],
    gemini: dict[str, Any],
) -> None:
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "stored.txt").write_text(
        _render_transcript(f"{title} — stored Meeting Mode transcript", stored),
        encoding="utf-8",
    )
    (meeting_dir / "current_redecode_polish.txt").write_text(
        _render_transcript(
            f"{title} — current End: Whisper re-transcription + Gemini cleanup",
            current.get("segments") or [],
            extra_header=(
                f"redecode {current.get('redecode', {}).get('elapsed_s', 0):.1f}s, "
                f"polish {current.get('polish', {}).get('elapsed_s', 0):.1f}s"
            ),
        ),
        encoding="utf-8",
    )
    (meeting_dir / "gemini_audio.txt").write_text(
        _render_transcript(
            f"{title} — Gemini multimodal on full meeting audio",
            gemini.get("segments") or [],
            extra_header=(
                f"{gemini.get('model')} in {gemini.get('elapsed_s', 0):.1f}s"
            ),
        ),
        encoding="utf-8",
    )


def _summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Gemini-on-audio vs re-transcription + cleanup",
        "",
        f"- Created: `{summary['created_at']}`",
        f"- Current path: Whisper `{summary['asr_model']}` redecode + "
        f"`{summary['polish_model']}` polish",
        f"- Gemini audio path: `{summary['gemini_model']}` on the full meeting MP3",
        f"- Meetings: {len(summary['meetings'])}",
        "",
        "| Meeting | Minutes | Current words | Gemini words | "
        "Current vs stored WER | Gemini vs stored WER | Gemini vs current WER | "
        "Current s | Gemini s | Gemini cost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["meetings"]:
        lines.append(
            f"| {item['title'] or item['meeting_id']} | "
            f"{item['audio_s'] / 60.0:.1f} | "
            f"{item['current']['word_count']} | "
            f"{item['gemini']['word_count']} | "
            f"{item['scores']['current_vs_stored']['wer']:.2%} | "
            f"{item['scores']['gemini_vs_stored']['wer']:.2%} | "
            f"{item['scores']['gemini_vs_current']['wer']:.2%} | "
            f"{item['current']['elapsed_s']:.1f} | "
            f"{item['gemini']['elapsed_s']:.1f} | "
            f"{item['gemini'].get('usage', {}).get('cost', '—')} |"
        )
    lines.extend([
        "",
        "WER here is untimed token Levenshtein. There is no human reference, "
        "so stored-vs-X measures change from the already-finalized Meeting Mode "
        "transcript, and Gemini-vs-current measures how far the two completed "
        "pipelines diverge. Lower is closer, not automatically better.",
        "",
        "Per-meeting completed transcripts are in `stored.txt`, "
        "`current_redecode_polish.txt`, and `gemini_audio.txt`.",
        "",
    ])
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "openwhisper.db",
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results"
        / "gemini-audio-vs-redecode",
    )
    parser.add_argument(
        "--meetings",
        default=",".join(DEFAULT_MEETING_IDS),
        help="Comma-separated meeting ids",
    )
    parser.add_argument("--asr-model", default="")
    parser.add_argument("--gemini-model", default="")
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--force", action="store_true", help="Compatibility flag; requested arms are always recomputed")
    parser.add_argument("--skip-current", action="store_true")
    parser.add_argument("--skip-gemini", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    from services.settings import (
        resolve_meeting_language,
        resolve_meeting_llm_model,
        resolve_meeting_llm_provider,
        resolve_meeting_whisper_model,
        settings_manager,
    )
    from transcriber.local_backend import LocalWhisperBackend

    settings = settings_manager.load_all_settings()
    asr_model = args.asr_model or resolve_meeting_whisper_model(settings)
    language = resolve_meeting_language(settings)
    language_code = None if language == "auto" else language
    provider = resolve_meeting_llm_provider(settings)
    polish_model = resolve_meeting_llm_model(settings)
    gemini_model = args.gemini_model or polish_model
    api_key = find_api_key(provider)
    if not api_key:
        print(f"No API key for {provider}", file=sys.stderr)
        return 2

    ids = [item.strip() for item in args.meetings.split(",") if item.strip()]
    args.results_dir.mkdir(parents=True, exist_ok=True)
    conn = _connect(args.db)
    backend = None
    from benchmarks.provenance import identity
    provenance = identity(settings={
        **{key: str(value) for key, value in vars(args).items()},
        "provider": provider, "polish_model": polish_model,
        "gemini_model": gemini_model, "asr_model": asr_model, "language": language,
    })
    results: list[dict[str, Any]] = []
    try:
        meetings = [_load_meeting(conn, meeting_id) for meeting_id in ids]
        for meeting in meetings:
            wav = _session_wav(meeting, args.root)
            meeting["audio_path"] = str(wav)
            meeting["audio_s"] = _wav_duration_s(wav)
        if not args.skip_current:
            print(f"Loading Whisper {asr_model}...", flush=True)
            backend = LocalWhisperBackend(model_name=asr_model)
            if not backend.is_available() or backend.model is None:
                print(f"Whisper model unavailable: {asr_model}", file=sys.stderr)
                return 1

        for index, meeting in enumerate(meetings, start=1):
            meeting_id = meeting["id"]
            title = meeting.get("title") or meeting_id
            meeting_dir = args.results_dir / meeting_id
            result_path = meeting_dir / "result.json"
            print(
                f"[{index}/{len(meetings)}] {meeting_id}: {title} "
                f"({meeting['audio_s'] / 60.0:.1f} min)",
                flush=True,
            )

            stored = [_segment_dict(seg) for seg in meeting["stored_segments"]]
            current: dict[str, Any] = {
                "ok": False,
                "elapsed_s": 0.0,
                "word_count": 0,
                "segments": [],
                "redecode": {},
                "polish": {},
            }
            if not args.skip_current:
                print("  redecode...", flush=True)
                redecode = _run_redecode(
                    meeting,
                    root=args.root,
                    model_name=asr_model,
                    language=language_code or "en",
                    backend=backend,
                )
                print(
                    f"  redecode {redecode['word_count']} words in "
                    f"{redecode['elapsed_s']:.1f}s (RTF {redecode['rtf']:.3f})",
                    flush=True,
                )
                print("  polish...", flush=True)
                polish = _run_polish(
                    meeting_id,
                    redecode["segments"],
                    provider=provider,
                    model=polish_model,
                    api_key=api_key,
                )
                print(
                    f"  polish {polish['word_count']} words in "
                    f"{polish['elapsed_s']:.1f}s ok={polish['ok']}",
                    flush=True,
                )
                current = {
                    "ok": bool(redecode.get("ok") and polish.get("ok")),
                    "elapsed_s": float(redecode["elapsed_s"]) + float(polish["elapsed_s"]),
                    "word_count": polish["word_count"],
                    "segments": polish["segments"],
                    "redecode": {
                        key: value for key, value in redecode.items()
                        if key != "segments"
                    },
                    "polish": {
                        key: value for key, value in polish.items()
                        if key != "segments"
                    },
                    "redecode_segments": redecode["segments"],
                }

            gemini: dict[str, Any] = {
                "ok": False,
                "elapsed_s": 0.0,
                "word_count": 0,
                "segments": [],
                "usage": {},
                "model": gemini_model,
            }
            if not args.skip_gemini:
                mp3_path = meeting_dir / "meeting.mp3"
                print("  compress audio...", flush=True)
                compression = _compress_mp3(Path(meeting["audio_path"]), mp3_path)
                print(
                    f"  mp3 {compression['bytes']} bytes "
                    f"(from {compression['source_bytes']})",
                    flush=True,
                )
                print("  gemini audio...", flush=True)
                gemini = _run_gemini_audio(
                    mp3_path,
                    model=gemini_model,
                    api_key=api_key,
                    timeout_s=args.timeout_s,
                )
                gemini["compression"] = compression
                print(
                    f"  gemini {gemini['word_count']} words in "
                    f"{gemini['elapsed_s']:.1f}s finish={gemini.get('finish_reason')} "
                    f"cost={gemini.get('usage', {}).get('cost')}",
                    flush=True,
                )

            stored_text = _plain_text(stored)
            current_text = _plain_text(current.get("segments") or [])
            gemini_text = _plain_text(gemini.get("segments") or [])
            from benchmarks.provenance import file_identity
            current["enabled"] = not args.skip_current
            gemini["enabled"] = not args.skip_gemini
            result = {
                "provenance": {**provenance, "audio_sha256": file_identity(meeting["audio_path"])},
                "meeting_id": meeting_id,
                "title": title,
                "audio_s": meeting["audio_s"],
                "audio_path": meeting["audio_path"],
                "asr_model": asr_model,
                "polish_model": polish_model,
                "gemini_model": gemini_model,
                "stored_word_count": len(normalize_tokens(stored_text)),
                "stored_segments": stored,
                "current": current,
                "gemini": {
                    key: value for key, value in gemini.items()
                    if key != "raw_text"
                },
                "gemini_raw_text": gemini.get("raw_text") or "",
                "scores": {
                    "current_vs_stored": _compare_texts(stored_text, current_text),
                    "gemini_vs_stored": _compare_texts(stored_text, gemini_text),
                    "gemini_vs_current": _compare_texts(current_text, gemini_text),
                },
            }
            meeting_dir.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _write_meeting_outputs(meeting_dir, title, stored, current, gemini)
            results.append(result)
    finally:
        if backend is not None:
            try:
                backend.cleanup()
            except Exception:
                logging.exception("Whisper cleanup failed")
        conn.close()

    summary = {
        "created_at": _utc_now(),
        "asr_model": asr_model,
        "polish_model": polish_model,
        "gemini_model": gemini_model,
        "meetings": [
            {
                "meeting_id": item["meeting_id"],
                "title": item["title"],
                "audio_s": item["audio_s"],
                "stored_word_count": item["stored_word_count"],
                "current": {
                    "ok": item["current"].get("ok"),
                    "elapsed_s": item["current"].get("elapsed_s"),
                    "word_count": item["current"].get("word_count"),
                },
                "gemini": {
                    "ok": item["gemini"].get("ok"),
                    "elapsed_s": item["gemini"].get("elapsed_s"),
                    "word_count": item["gemini"].get("word_count"),
                    "usage": item["gemini"].get("usage") or {},
                    "finish_reason": item["gemini"].get("finish_reason"),
                },
                "scores": item["scores"],
            }
            for item in results
        ],
    }
    (args.results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.results_dir / "summary.md").write_text(
        _summary_markdown(summary), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return int(any(
        (not args.skip_current and not item["current"].get("ok"))
        or (not args.skip_gemini and not item["gemini"].get("ok")) for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
