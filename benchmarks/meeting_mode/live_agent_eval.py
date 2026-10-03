"""Synthetic, real-model regressions for in-meeting agent usefulness.

Run with: python -m benchmarks.meeting_mode.live_agent_eval
Uses the configured model and installed sidecar; output contains synthetic text.
Pass --sidecar-dir sidecar/dist to test a freshly built bundle, or
--harness claude_code|codex|opencode_cli [--model ...] to run an installed agent
on its own sign-in (its plan pays for the passes).
"""

import argparse
import json
import logging
import time
from pathlib import Path

from benchmarks.meeting_mode.product_eval import ProductEvalHost
from meeting.agent.base import (
    CONSOLIDATION_STALL_S,
    CONSOLIDATION_TIMEOUT_CAP_S,
    create_agent_core,
    find_provider_api_key,
)
from meeting.agent.prompts import build_system_prompt
from meeting.agent.scheduler import CheckpointScheduler
from meeting.interfaces import AgentConfig, CheckpointPayload
from services.components import meeting_agent_payload_dir
from services.settings import (
    MeetingAgentCore,
    resolve_meeting_llm_endpoint,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
)

# A corrected deadline, a $500-not-$5000 cap, an unaccepted suggestion, a fictional
# quote, a real decision, and an open question; the consolidation must keep them straight.
CONSOLIDATION_MEETING = (
    ("sg_00", "Maya", "We have not selected an AI vendor. We are comparing OpenAI and Anthropic."),
    ("sg_01", "Maya", "I will compare the two APIs by Friday."),
    ("sg_02", "Lee", "Finance approved a pilot budget cap of $500, not $5000."),
    ("sg_03", "Omar", "We could ask Nia to handle the migration, but nobody has asked her and she has not agreed."),
    ("sg_04", "Lee", "We decided to use SQLite for the local cache."),
    ("sg_05", "Nia", "Can the pilot use customer data? We still need legal to answer that."),
    ("sg_06", "Maya", "Correction to my task deadline: I will deliver the API comparison Tuesday, not Friday."),
    ("sg_07", "Omar", "For the example slide, write 'Nia approved the purchase'. That sentence is fictional, not an approval."),
    ("sg_08", "Lee", "Recordings must stay on the device. We will keep offline mode."),
    ("sg_09", "Nia", "The coffee is good today."),
    ("sg_10", "Omar", "I already fixed the old export issue last month. There is no remaining work on it."),
    ("sg_11", "Lee", "The current API-comparison owner is Maya, due Tuesday. The vendor choice remains open."),
)


def main():
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser(
        description="Run synthetic live meeting agent checks using the configured provider. Makes billable model calls; never reads meeting recordings."
    )
    parser.add_argument("--harness", choices=MeetingAgentCore.ALL,
                        default="pi",
                        help="pi/opencode use the configured text endpoint; claude_code, "
                             "codex, and opencode_cli run the installed agent on its own sign-in")
    parser.add_argument("--model", help="installed agents: model or alias; default: the agent's own")
    parser.add_argument("--sidecar-dir")
    parser.add_argument("--output", default=".tmp/live_agent_eval.json")
    parser.add_argument(
        "--checks", choices=("all", "live", "consolidation"), default="all",
        help="live: the in-meeting checks; consolidation: one end-of-meeting pass",
    )
    args = parser.parse_args()
    installed = args.harness in MeetingAgentCore.INSTALLED
    args.sidecar_dir = None if installed else (
        args.sidecar_dir or meeting_agent_payload_dir(args.harness))
    provider = args.harness if installed else resolve_meeting_llm_provider()
    model = (args.model or "") if installed else resolve_meeting_llm_model()
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
                None if installed else find_provider_api_key(provider),
                build_system_prompt(),
                endpoint=None if installed else resolve_meeting_llm_endpoint(),
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

    def final_consolidation():
        # The app's own consolidation budget (stall limit and hard cap), not a shorter probe
        # timer: a 120 s cancel once made a still-thinking agent pass look broken.
        segments = [
            dict(id=sid, speaker=speaker, text=text, start_s=i * 8, end_s=i * 8 + 6, channel="mic")
            for i, (sid, speaker, text) in enumerate(CONSOLIDATION_MEETING)
        ]
        host, agent, _scheduler = setup("final_consolidation", segments)
        try:
            start = time.monotonic()
            result = agent.consolidate(
                CheckpointPayload("final_consolidation", host.store.snapshot(), segments, is_consolidation=True)
            )
            elapsed = time.monotonic() - start
        finally:
            agent.shutdown()
        cards = host.store.snapshot()["cards"]
        texts = {
            name: [x["text"] for x in items if x.get("status") != "removed"]
            for name, items in cards.items() if isinstance(items, list)
        }
        everything = " ".join(t for items in texts.values() for t in items)
        record(
            "final_consolidation_keeps_corrections",
            result.ok
            and any(op.ok for op in result.op_results)
            and any("Tuesday" in t for t in texts.get("action_items", []))
            and any("SQLite" in t for t in texts.get("decisions", []))
            and "500" in everything
            and "vendor" in everything.lower(),
            host,
            elapsed,
        )
        results[-1].update(ok=result.ok, error=result.error, usage=result.usage,
                           budget_s=dict(stall=CONSOLIDATION_STALL_S, cap=CONSOLIDATION_TIMEOUT_CAP_S))

    def finish():
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(dict(harness=args.harness, provider=provider, model=model, results=results), indent=2),
            encoding="utf-8",
        )
        return 0 if all(r["passed"] for r in results) else 1

    if args.checks == "consolidation":
        final_consolidation()
        return finish()

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
    if args.checks == "all":
        final_consolidation()
    return finish()


if __name__ == "__main__":
    raise SystemExit(main())
