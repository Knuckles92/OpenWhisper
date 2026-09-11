"""Synthetic, real-model regressions for in-meeting agent usefulness.

Run with: python -m benchmarks.meeting_mode.live_agent_eval
Uses the configured model and installed sidecar; output contains synthetic text.
Pass --sidecar-dir sidecar/dist to test a freshly built bundle.
"""

import argparse
import json
import logging
import time
from pathlib import Path

from benchmarks.meeting_mode.product_eval import ProductEvalHost
from meeting.agent.base import create_agent_core, find_provider_api_key
from meeting.agent.prompts import build_system_prompt
from meeting.agent.scheduler import CheckpointScheduler
from meeting.interfaces import AgentConfig
from services.components import meeting_agent_payload_dir
from services.settings import (
    resolve_meeting_llm_endpoint,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
)


def main():
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(
        description="Run synthetic live meeting agent checks using the configured provider. Makes billable model calls; never reads meeting recordings."
    )
    parser.add_argument("--harness", choices=("pi", "opencode", "direct"), default="pi")
    parser.add_argument("--sidecar-dir")
    parser.add_argument("--output", default=".tmp/live_agent_eval.json")
    args = parser.parse_args()
    args.sidecar_dir = args.sidecar_dir or meeting_agent_payload_dir(args.harness)
    provider = resolve_meeting_llm_provider()
    model = resolve_meeting_llm_model()
    results = []

    def segment(sid, t, text):
        return dict(id=sid, start_s=t, end_s=t + 4, text=text, channel="mic")

    def setup(name, segments):
        host = ProductEvalHost("synthetic_" + name, segments)
        host.store.update_runtime_fields(status="active")
        host.allow_agent_writes()
        agent = create_agent_core(args.harness, str(Path(args.sidecar_dir).resolve()) if args.sidecar_dir else None)
        agent.initialize(
            AgentConfig(
                host.meeting_id,
                provider,
                model,
                find_provider_api_key(provider),
                build_system_prompt(),
                endpoint=resolve_meeting_llm_endpoint(),
            ),
            host,
        )
        return host, agent, CheckpointScheduler(host, agent)

    def record(name, passed, host, elapsed):
        entry = dict(
            name=name,
            passed=passed,
            elapsed_s=round(elapsed, 2),
            transcript=host.get_transcript(),
            state=host.store.snapshot(),
        )
        results.append(entry)
        print(
            json.dumps(dict(name=name, passed=passed, elapsed_s=entry["elapsed_s"])),
            flush=True,
        )

    host, agent, scheduler = setup(
        "ai_vendor",
        [
            segment(
                "sg_vendor",
                1,
                "We are comparing OpenAI with Entropic, the company that makes Claude.",
            ),
            segment(
                "sg_plan",
                6,
                "We have not chosen a vendor. Maya will compare the two APIs by Friday. The budget cap is 500 dollars, not 5000.",
            ),
        ],
    )
    try:
        start = time.monotonic()
        scheduler._fire()
        state = host.store.snapshot()
        text = host.get_transcript()[0]["text"]
        record(
            "automatic_name_correction_and_live_notes",
            "Anthropic" in text
            and "Entropic" not in text
            and bool(state["cards"]["live_notes"]),
            host,
            time.monotonic() - start,
        )
        # A real quiet-period correction to an existing name should revisit notes.
        host.store.apply(
            "user",
            "p_test",
            [
                dict(
                    op="add_item",
                    card="user_notes",
                    text="The person assigned to compare APIs is spelled Maia, not Maya.",
                    data={"kind": "agent_insight"},
                    evidence=["sg_plan"],
                )
            ],
        )
        start = time.monotonic()
        scheduler.notify_guidance()
        scheduler._fire()
        notes = " ".join(
            x["text"]
            for x in host.store.snapshot()["cards"]["live_notes"]
            if x["status"] != "removed"
        )
        record(
            "human_guidance_updates_existing_notes",
            "Maia" in notes and "Maya" not in notes,
            host,
            time.monotonic() - start,
        )
        # Exercise actual queue and Future without incoming speech.
        scheduler.start()
        start = time.monotonic()
        response = scheduler.request_note_adjustment(
            "Rewrite the existing AI notes as bullet points. Keep the budget cap, the pending vendor choice, the owner, and Friday deadline."
        ).result(timeout=90)
        notes = " ".join(
            x["text"]
            for x in host.store.snapshot()["cards"]["live_notes"]
            if x["status"] != "removed"
        )
        record(
            "explicit_note_request_without_speech",
            response.ok
            and any(x.ok for x in response.op_results)
            and ("- " in notes or "•" in notes)
            and "500" in notes and "5000" not in notes
            and "Maia" in notes and "Maya" not in notes
            and "Friday" in notes
            and "vendor" in notes.lower()
            and any(term in notes.lower() for term in ("pending", "not chosen", "undecided", "not selected", "no vendor", "unselected")),
            host,
            time.monotonic() - start,
        )
    finally:
        scheduler.stop()
        agent.shutdown()
    host, agent, scheduler = setup(
        "physics",
        [
            segment(
                "sg_physics",
                1,
                "Entropic forces arise from entropy. The polymer contracts because of an entropic effect, not because we changed the temperature.",
            )
        ],
    )
    try:
        start = time.monotonic()
        scheduler._fire()
        text = host.get_transcript()[0]["text"]
        record(
            "valid_entropic_is_preserved",
            "entropic" in text.lower() and "Anthropic" not in text,
            host,
            time.monotonic() - start,
        )
    finally:
        agent.shutdown()
    host, agent, scheduler = setup(
        "later_context",
        [segment("sg_earlier", 1, "We should look at Entropic as another option.")],
    )
    try:
        start = time.monotonic()
        scheduler._fire()
        record(
            "ambiguous_name_is_not_guessed",
            host.get_transcript()[0]["text"]
            == "We should look at Entropic as another option.",
            host,
            time.monotonic() - start,
        )
        host._segments["sg_later"] = segment(
            "sg_later",
            10,
            "I mean the AI company behind Claude. Compare their API with OpenAI, but we are only exploring options.",
        )
        start = time.monotonic()
        scheduler._fire()
        text = host.get_transcript()[0]["text"]
        record(
            "later_context_repairs_earlier_line",
            "Anthropic" in text,
            host,
            time.monotonic() - start,
        )
    finally:
        agent.shutdown()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(dict(harness=args.harness, provider=provider, model=model, results=results), indent=2),
        encoding="utf-8",
    )
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
