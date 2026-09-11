"""Test whether full-meeting Gemini audio beats text consolidation on insights.

Reuses the three real meetings and finished transcripts from
``gemini_audio_eval``. Three End packages are built:

* current — production consolidation on the Whisper+polish transcript
* audio — Gemini hears only the full meeting MP3
* audio+text — Gemini hears the MP3 and also reads the current transcript

An LLM judge scores current vs audio and current vs audio+text. The second
pair isolates whether hearing adds anything when the word list is already
good. The production database is read-only.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app_qt as _app_bootstrap  # noqa: E402,F401

from benchmarks.meeting_mode.gemini_audio_eval import (  # noqa: E402
    DEFAULT_MEETING_IDS,
    _compress_mp3,
    _plain_text,
    _session_wav,
    _usage_dict,
    _wav_duration_s,
)
from benchmarks.meeting_mode.metrics import normalize_tokens  # noqa: E402
from benchmarks.meeting_mode.product_eval import (  # noqa: E402
    _render_package_for_judge,
    run_product_pipeline,
)
from meeting.state.schema import CARD_KEYS  # noqa: E402
from services.transcript_cleanup import find_api_key  # noqa: E402

INSIGHTS_PROMPT = """\
You are writing the durable meeting record a participant would keep after End.

Listen to the entire recording. If a transcript is also provided, use it as
a second witness — prefer the audio when they disagree.

Return JSON only, no markdown fences:
{
  "topic": "short subject line",
  "summary": "complete summary of the whole recording",
  "key_points": [{"text": "...", "start_s": 0.0}],
  "decisions": [{"text": "..."}],
  "action_items": [{"text": "...", "owner": ""}],
  "risks": [{"text": "...", "severity": "low|medium|high"}],
  "timeline": [{"text": "story beat", "start_s": 0.0}],
  "live_notes": [{"heading": "short label", "start_s": 0.0, "text": "minutes"}],
  "questions": [{"text": "...", "status": "open", "answer_text": ""}],
  "segments": [{"start_s": 0.0, "end_s": 4.0, "speaker": "A", "text": "..."}]
}

Rules:
- Transcribe all audible speech into segments. Do not skip stretches.
- A decision requires spoken agreement or a real commitment. A YouTube
  monologue, a described workflow, or a preference is NOT a decision —
  put those in key_points. Empty decisions/action_items is correct when
  nobody agreed to do anything.
- Connect early framing to later payoff when the recording actually does that.
- Do not invent names, numbers, owners, or follow-ups.
- start_s is seconds from the start of this audio file.
"""

JUDGE_SYSTEM = """\
You compare meeting-record packages. There is no human gold transcript.
You get two independent transcripts of the same recording (local ASR+cleanup,
and Gemini-from-audio) plus two End packages.

Score only the durable record: topic, summary, cards, notes, questions.
Do not reward a longer card list. Empty decisions/actions is correct when
the recording is a talk or monologue with no spoken commitment.
Penalize invented decisions, owners, or claims neither transcript supports.
Reward a package that ties an opening framing to a later conclusion only
when both transcripts support that connection.

Return JSON only."""

JUDGE_RUBRIC = """\
## MEETING
{meeting_id}: {title}

## TRANSCRIPT X — local re-decode + cleanup
{transcript_current}

## TRANSCRIPT Y — Gemini from the audio file
{transcript_audio}

## PACKAGE A
{package_a}

## PACKAGE B
{package_b}

Score each package 1-5 on:
- transcript_usefulness
- topic_accuracy
- key_points_fidelity
- decisions_actions_precision
- notes_and_timeline_quality
- longer_horizon: opening framed to later payoff, only if the recording does that
- overall_record: which package would you keep

Return:
{{
  "a": {{"transcript_usefulness": n, "topic_accuracy": n,
        "key_points_fidelity": n, "decisions_actions_precision": n,
        "notes_and_timeline_quality": n, "longer_horizon": n,
        "overall_record": n}},
  "b": {{...same keys...}},
  "winner": "a" | "b" | "tie",
  "horizon_example": "one supported early-to-late connection, or empty",
  "invented_claims": ["claims in either package that neither transcript supports"],
  "rationale": "short paragraph"
}}
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _items_to_cards(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    cards: dict[str, list[dict[str, Any]]] = {}
    for key in CARD_KEYS:
        if key == "user_notes":
            continue
        rows = payload.get(key) or []
        cards[key] = []
        for row in rows:
            if isinstance(row, str):
                text = row.strip()
                data: dict[str, Any] = {}
            elif isinstance(row, dict):
                text = (row.get("text") or "").strip()
                data = {}
                if row.get("start_s") is not None:
                    data["start_s"] = float(row["start_s"])
                if row.get("heading"):
                    data["heading"] = row["heading"]
                if row.get("owner"):
                    data["owner_participant_id"] = row["owner"]
                if row.get("severity"):
                    data["severity"] = row["severity"]
            else:
                continue
            if text:
                cards[key].append({"text": text, "data": data})
    return cards


def _parse_json_object(text: str) -> dict[str, Any]:
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
            raise
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Gemini insights response was not an object")
    return payload


def _package_from_gemini(payload: dict[str, Any]) -> dict[str, Any]:
    segments = []
    for index, row in enumerate(payload.get("segments") or []):
        if not isinstance(row, dict):
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "id": f"gemini_{index:04d}",
            "start_s": float(row.get("start_s") or 0.0),
            "end_s": float(row.get("end_s") or 0.0),
            "text": text,
            "speaker": (row.get("speaker") or "").strip(),
        })
    questions = []
    for row in payload.get("questions") or []:
        if isinstance(row, str) and row.strip():
            questions.append({
                "text": row.strip(), "status": "open", "answer_text": "",
            })
        elif isinstance(row, dict) and (row.get("text") or "").strip():
            questions.append({
                "text": row["text"].strip(),
                "status": row.get("status") or "open",
                "answer_text": row.get("answer_text") or "",
            })
    return {
        "title": (payload.get("topic") or "").strip(),
        "topic": (payload.get("topic") or "").strip(),
        "summary": (payload.get("summary") or "").strip(),
        "cards": _items_to_cards(payload),
        "questions": questions,
        "transcript": segments,
    }


def _trim_package_for_disk(package: dict[str, Any]) -> dict[str, Any]:
    keep = dict(package)
    keep.pop("compact", None)
    keep.pop("polish", None)
    keep.pop("consolidation", None)
    keep.pop("op_stats", None)
    return keep


def _render_package_md(title: str, package: dict[str, Any]) -> str:
    return f"# {title}\n\n{_render_package_for_judge(package)}\n"


def _card_counts(package: dict[str, Any]) -> dict[str, int]:
    cards = package.get("cards") or {}
    counts = {
        key: len(cards.get(key) or [])
        for key in CARD_KEYS
        if key != "user_notes"
    }
    counts["questions"] = len(package.get("questions") or [])
    return counts


def _lexical_overlap(package: dict[str, Any], transcripts: list[str]) -> dict[str, Any]:
    corpus = set()
    for text in transcripts:
        corpus.update(normalize_tokens(text))
    claims: list[str] = []
    if package.get("topic"):
        claims.append(str(package["topic"]))
    if package.get("summary"):
        claims.append(str(package["summary"]))
    for items in (package.get("cards") or {}).values():
        for item in items or []:
            if isinstance(item, dict) and item.get("text"):
                claims.append(str(item["text"]))
    supported = 0
    weak = 0
    for claim in claims:
        tokens = normalize_tokens(claim)
        content = [tok for tok in tokens if len(tok) > 3]
        if not content:
            continue
        hit = sum(1 for tok in content if tok in corpus)
        if hit / max(1, len(content)) >= 0.4:
            supported += 1
        else:
            weak += 1
    total = supported + weak
    return {
        "claims": total,
        "high_overlap": supported,
        "low_overlap": weak,
        "high_overlap_rate": supported / total if total else None,
        "interpretation": "Lexical overlap only; does not measure factual support or contradiction",
    }


def _call_gemini(
    *,
    mp3_path: Path,
    model: str,
    api_key: str,
    timeout_s: float,
    extra_text: str = "",
) -> dict[str, Any]:
    from openai import OpenAI

    audio_b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": INSIGHTS_PROMPT},
    ]
    if extra_text.strip():
        content.append({
            "type": "text",
            "text": "Independent local transcript (second witness):\n" + extra_text,
        })
    content.append({
        "type": "input_audio",
        "input_audio": {"data": audio_b64, "format": "mp3"},
    })
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "OpenWhisper gemini-audio insights"},
        timeout=timeout_s,
    )
    started = time.perf_counter()
    last_error = ""
    response = None
    text = ""
    finish = ""
    for attempt in range(2):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 32768,
                "temperature": 0,
            }
            if attempt == 0:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(2.0)
                continue
            return {
                "ok": False,
                "elapsed_s": time.perf_counter() - started,
                "error": last_error,
                "usage": {},
                "raw_text": "",
                "package": {},
            }
        choice = (response.choices or [None])[0]
        if choice is not None:
            text = (choice.message.content or "") if choice.message else ""
            finish = choice.finish_reason or ""
        extra = getattr(response, "error", None)
        if extra:
            last_error = str(extra)
        if text.strip() and finish not in {"error", "content_filter"}:
            break
        last_error = last_error or f"empty response (finish={finish or 'unknown'})"
        if attempt == 0:
            time.sleep(2.0)
    elapsed = time.perf_counter() - started
    usage = _usage_dict(getattr(response, "usage", None) if response else None)
    try:
        package = _package_from_gemini(_parse_json_object(text))
        ok = True
    except Exception as exc:
        package = {}
        ok = False
        last_error = str(exc)
    return {
        "ok": ok and finish not in {"length", "content_filter", "error"},
        "elapsed_s": elapsed,
        "finish_reason": finish,
        "error": last_error,
        "usage": usage,
        "raw_text": text,
        "package": package,
    }


def _judge(
    *,
    meeting_id: str,
    title: str,
    transcript_current: str,
    transcript_audio: str,
    package_a: dict[str, Any],
    package_b: dict[str, Any],
    model: str,
    api_key: str,
) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "OpenWhisper insights judge"},
        timeout=120.0,
    )
    user = JUDGE_RUBRIC.format(
        meeting_id=meeting_id,
        title=title,
        transcript_current=transcript_current[:14000],
        transcript_audio=transcript_audio[:14000],
        package_a=_render_package_for_judge(package_a, max_transcript_chars=4000),
        package_b=_render_package_for_judge(package_b, max_transcript_chars=4000),
    )
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        max_tokens=4096,
    )
    content = (response.choices[0].message.content or "").strip()
    try:
        judgment = json.loads(content)
    except json.JSONDecodeError:
        judgment = {"winner": "unjudged", "rationale": content, "parse_error": True}
    if not isinstance(judgment, dict) or judgment.get("winner") not in ("a", "b", "A", "B", "tie") or not all(isinstance(judgment.get(side), dict) and judgment[side] for side in ("a", "b")):
        judgment = {"winner": "unjudged", "parse_error": True, "raw_judgment": judgment}
    judgment["elapsed_s"] = time.perf_counter() - started
    judgment["usage"] = _usage_dict(getattr(response, "usage", None))
    return judgment


def _load_prior(transcript_root: Path, meeting_id: str) -> dict[str, Any]:
    path = transcript_root / meeting_id / "result.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing prior transcript result {path}. "
            "Run benchmarks.meeting_mode.gemini_audio_eval first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Gemini audio insights vs text consolidation",
        "",
        f"- Created: `{summary['created_at']}`",
        f"- Model: `{summary['model']}`",
        f"- Meetings: {len(summary['meetings'])}",
        "",
        "| Meeting | Current cards | Audio cards | Audio+text cards | "
        "Judge current vs audio | Judge current vs audio+text | "
        "Current s | Audio s | Audio+text s | Audio cost |",
        "|---|---:|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for item in summary["meetings"]:
        lines.append(
            f"| {item['title']} | "
            f"{item['current']['card_total']} | "
            f"{item['audio']['card_total']} | "
            f"{item['audio_plus_text']['card_total']} | "
            f"{item['judgments']['current_vs_audio'].get('winner', '—')} | "
            f"{item['judgments']['current_vs_audio_plus_text'].get('winner', '—')} | "
            f"{item['current']['elapsed_s']:.1f} | "
            f"{item['audio']['elapsed_s']:.1f} | "
            f"{item['audio_plus_text']['elapsed_s']:.1f} | "
            f"{item['audio'].get('cost', '—')} |"
        )
    lines.extend(["", "## Per meeting", ""])
    for item in summary["meetings"]:
        lines.append(f"### {item['title']}")
        lines.append("")
        for name, judgment in item["judgments"].items():
            lines.append(
                f"- `{name}`: winner=`{judgment.get('winner')}` "
                f"horizon={judgment.get('horizon_example')!r}"
            )
            if judgment.get("rationale"):
                lines.append(f"  - {judgment['rationale']}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results"
        / "gemini-audio-vs-redecode",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results"
        / "gemini-audio-insights",
    )
    parser.add_argument("--meetings", default=",".join(DEFAULT_MEETING_IDS))
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--force", action="store_true", help="Compatibility flag; requested arms are always recomputed")
    parser.add_argument("--skip-audio-plus-text", action="store_true")
    parser.add_argument("--skip-judge", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from services.settings import (
        resolve_meeting_llm_model,
        resolve_meeting_llm_provider,
        settings_manager,
    )

    settings = settings_manager.load_all_settings()
    provider = resolve_meeting_llm_provider(settings)
    model = args.model or resolve_meeting_llm_model(settings)
    api_key = find_api_key(provider)
    if not api_key:
        print(f"No API key for {provider}", file=sys.stderr)
        return 2

    ids = [item.strip() for item in args.meetings.split(",") if item.strip()]
    args.results_dir.mkdir(parents=True, exist_ok=True)
    from benchmarks.provenance import identity
    provenance = identity(settings={**{key: str(value) for key, value in vars(args).items()},
                                    "provider": provider, "model": model})
    results: list[dict[str, Any]] = []

    for index, meeting_id in enumerate(ids, start=1):
        prior = _load_prior(args.transcript_dir, meeting_id)
        title = prior.get("title") or meeting_id
        meeting_dir = args.results_dir / meeting_id
        result_path = meeting_dir / "result.json"
        print(f"[{index}/{len(ids)}] {meeting_id}: {title}", flush=True)

        current_segments = (prior.get("current") or {}).get("segments") or []
        if not current_segments:
            print(f"  no current transcript in {meeting_id}", file=sys.stderr)
            return 2
        audio_s = float(prior.get("audio_s") or 0.0)
        mp3_path = args.transcript_dir / meeting_id / "meeting.mp3"
        if not mp3_path.is_file():
            from benchmarks.meeting_mode.gemini_audio_eval import _connect, _load_meeting

            conn = _connect(PROJECT_ROOT / "openwhisper.db")
            try:
                meeting = _load_meeting(conn, meeting_id)
            finally:
                conn.close()
            wav = _session_wav(meeting, PROJECT_ROOT)
            audio_s = _wav_duration_s(wav)
            mp3_path = meeting_dir / "meeting.mp3"
            print("  compress audio...", flush=True)
            _compress_mp3(wav, mp3_path)

        print("  current consolidation...", flush=True)
        started = time.perf_counter()
        current_pkg = run_product_pipeline(
            meeting_id,
            current_segments,
            polish=False,
            provider=provider,
            model=model,
            api_key=api_key,
        )
        current_elapsed = time.perf_counter() - started
        print(
            f"  current consolidation {current_elapsed:.1f}s "
            f"status={((current_pkg.get('consolidation') or {}).get('status'))}",
            flush=True,
        )

        current_text = _plain_text(current_segments)
        print("  gemini audio insights...", flush=True)
        audio_run = _call_gemini(
            mp3_path=mp3_path,
            model=model,
            api_key=api_key,
            timeout_s=args.timeout_s,
        )
        print(
            f"  audio insights {audio_run['elapsed_s']:.1f}s "
            f"ok={audio_run['ok']} cost={audio_run.get('usage', {}).get('cost')}",
            flush=True,
        )

        plus_run: dict[str, Any] = {
            "ok": False, "elapsed_s": 0.0, "usage": {}, "package": {},
            "error": "skipped",
        }
        if not args.skip_audio_plus_text:
            print("  gemini audio+text insights...", flush=True)
            plus_run = _call_gemini(
                mp3_path=mp3_path,
                model=model,
                api_key=api_key,
                timeout_s=args.timeout_s,
                extra_text=current_text,
            )
            print(
                f"  audio+text insights {plus_run['elapsed_s']:.1f}s "
                f"ok={plus_run['ok']} cost={plus_run.get('usage', {}).get('cost')}",
                flush=True,
            )

        audio_pkg = audio_run.get("package") or {}
        plus_pkg = plus_run.get("package") or {}
        audio_text = _plain_text(audio_pkg.get("transcript") or [])
        if not audio_text:
            audio_text = _plain_text((prior.get("gemini") or {}).get("segments") or [])

        judgments = {
            "current_vs_audio": {},
            "current_vs_audio_plus_text": {},
        }
        if not args.skip_judge:
            print("  judge current vs audio...", flush=True)
            raw = _judge(
                meeting_id=meeting_id,
                title=title,
                transcript_current=current_text,
                transcript_audio=audio_text,
                package_a=_trim_package_for_disk(current_pkg),
                package_b=audio_pkg,
                model=model,
                api_key=api_key,
            )
            winner = {"a": "current", "b": "audio"}.get(
                str(raw.get("winner") or ""), raw.get("winner"),
            )
            raw["mapped_winner"] = winner
            judgments["current_vs_audio"] = raw
            print(f"  judge winner={winner}", flush=True)
            if plus_pkg:
                print("  judge current vs audio+text...", flush=True)
                raw_plus = _judge(
                    meeting_id=meeting_id,
                    title=title,
                    transcript_current=current_text,
                    transcript_audio=audio_text,
                    package_a=_trim_package_for_disk(current_pkg),
                    package_b=plus_pkg,
                    model=model,
                    api_key=api_key,
                )
                winner_plus = {"a": "current", "b": "audio+text"}.get(
                    str(raw_plus.get("winner") or ""), raw_plus.get("winner"),
                )
                raw_plus["mapped_winner"] = winner_plus
                judgments["current_vs_audio_plus_text"] = raw_plus
                print(f"  judge winner={winner_plus}", flush=True)

        current_clean = _trim_package_for_disk(current_pkg)
        transcripts = [current_text, audio_text]
        meeting_dir.mkdir(parents=True, exist_ok=True)
        (meeting_dir / "current_package.json").write_text(
            json.dumps(current_clean, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        (meeting_dir / "current_package.md").write_text(
            _render_package_md(f"{title} — current text consolidation", current_clean),
            encoding="utf-8",
        )
        (meeting_dir / "audio_package.json").write_text(
            json.dumps(audio_pkg, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        (meeting_dir / "audio_package.md").write_text(
            _render_package_md(f"{title} — Gemini full-audio insights", audio_pkg),
            encoding="utf-8",
        )
        (meeting_dir / "audio_plus_text_package.json").write_text(
            json.dumps(plus_pkg, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        (meeting_dir / "audio_plus_text_package.md").write_text(
            _render_package_md(
                f"{title} — Gemini audio + current transcript insights", plus_pkg,
            ),
            encoding="utf-8",
        )

        def _arm(name: str, elapsed: float, package: dict[str, Any], usage: dict[str, Any], ok: bool) -> dict[str, Any]:
            counts = _card_counts(package) if package else {}
            return {
                "ok": ok,
                "enabled": name != "audio_plus_text" or not args.skip_audio_plus_text,
                "elapsed_s": elapsed,
                "cost": usage.get("cost"),
                "usage": usage,
                "counts": counts,
                "card_total": sum(counts.values()) if counts else 0,
                "lexical_overlap": _lexical_overlap(package, transcripts) if package else {},
            }

        from benchmarks.provenance import file_identity
        result = {
            "provenance": {**provenance, "audio_sha256": file_identity(mp3_path),
                           "transcript_sha256": file_identity(args.transcript_dir / meeting_id / "result.json")},
            "meeting_id": meeting_id,
            "title": title,
            "audio_s": audio_s,
            "model": model,
            "current": _arm(
                "current", current_elapsed, current_clean, {},
                (current_pkg.get("consolidation") or {}).get("status") == "completed",
            ),
            "audio": _arm(
                "audio",
                float(audio_run.get("elapsed_s") or 0.0),
                audio_pkg,
                audio_run.get("usage") or {},
                bool(audio_run.get("ok")),
            ),
            "audio_plus_text": _arm(
                "audio_plus_text",
                float(plus_run.get("elapsed_s") or 0.0),
                plus_pkg,
                plus_run.get("usage") or {},
                bool(plus_run.get("ok")),
            ),
            "judgments": {
                key: {
                    "winner": value.get("mapped_winner") or value.get("winner"),
                    "a": value.get("a"),
                    "b": value.get("b"),
                    "horizon_example": value.get("horizon_example"),
                    "invented_claims": value.get("invented_claims"),
                    "rationale": value.get("rationale"),
                    "parse_error": value.get("parse_error"),
                }
                for key, value in judgments.items()
            },
        }
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        (meeting_dir / "judgments.json").write_text(
            json.dumps(judgments, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        results.append(result)

    summary = {
        "created_at": _utc_now(),
        "model": model,
        "meetings": results,
    }
    (args.results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    _write_summary(args.results_dir / "summary.md", summary)
    print(json.dumps({
        "created_at": summary["created_at"],
        "model": model,
        "meetings": [
            {
                "title": item["title"],
                "judgments": item["judgments"],
                "current": item["current"],
                "audio": {
                    k: item["audio"][k]
                    for k in ("ok", "elapsed_s", "cost", "card_total", "lexical_overlap")
                },
                "audio_plus_text": {
                    k: item["audio_plus_text"][k]
                    for k in ("ok", "elapsed_s", "cost", "card_total", "lexical_overlap")
                },
            }
            for item in results
        ],
    }, indent=2), flush=True)
    return int(any(
        not item["current"]["ok"] or not item["audio"]["ok"]
        or (not args.skip_audio_plus_text and not item["audio_plus_text"]["ok"])
        or any(j.get("parse_error") for j in item["judgments"].values())
        for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
