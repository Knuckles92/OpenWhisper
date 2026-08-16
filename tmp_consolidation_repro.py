"""Scratch repro: why do consolidation ops fail with unknown_evidence?

Replays the live phase once (cached to disk), then runs consolidation-only
passes and dumps every rejected op's evidence ids against the real segment
ids so the failure mode (truncation / wrong set / invention) is visible.
"""
from __future__ import annotations

import difflib
import json
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.meeting_mode.product_eval import (  # noqa: E402
    ProductEvalHost,
    simulate_live_meeting,
)
from config import config  # noqa: E402
from meeting.agent.base import create_agent_core, find_provider_api_key  # noqa: E402
from meeting.agent.prompts import build_system_prompt  # noqa: E402
from meeting.agent.scheduler import ConsolidationOutcome  # noqa: E402
from meeting.interfaces import AgentConfig, CheckpointPayload  # noqa: E402

RESULTS = (
    PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results"
    / "auto-auto-draft-only-t5-m20-p50-offline" / "IN1009.json"
)
CACHE = PROJECT_ROOT / "tmp_live_state_IN1009.json"


def load_segments():
    raw = json.loads(RESULTS.read_text(encoding="utf-8"))
    return raw["draft_segments"], raw["offline_segments"]


def get_live_state(draft, provider, model, api_key):
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    live = simulate_live_meeting(
        "IN1009", draft,
        provider=provider, model=model, api_key=api_key,
        window_s=120.0,
    )
    CACHE.write_text(json.dumps(live["state"], indent=1), encoding="utf-8")
    return live["state"]


def run_consolidation(segments, live_state, provider, model, api_key, label):
    from meeting.agent import openrouter_direct as direct_mod

    direct_mod._CONSOLIDATION_TIMEOUT_S = 360.0
    host = ProductEvalHost("IN1009", segments, initial_state=live_state)
    agent = create_agent_core("direct")
    agent.initialize(
        AgentConfig(
            meeting_id="IN1009", provider=provider, model=model,
            api_key=api_key, system_prompt=build_system_prompt(),
        ),
        host,
    )
    assert agent.is_healthy()
    host.allow_agent_writes()
    try:
        payload = CheckpointPayload(
            request_id=uuid.uuid4().hex,
            state_snapshot=host.store.snapshot(),
            new_segments=host.get_transcript(),
            is_consolidation=True,
        )
        result = agent.consolidate(payload)
    finally:
        host.revoke_agent_writes()
        agent.shutdown()
    known = set(host._segments)
    applied = rejected = 0
    print(f"\n=== {label}: segment ids known to host: {len(known)}")
    for item in result.op_results:
        if item.ok:
            applied += 1
            continue
        rejected += 1
        op = item.op or {}
        ev = op.get("evidence") or []
        print(f"REJECT {op.get('op')} reason={item.reason}")
        for ev_id in ev[:4]:
            if ev_id in known:
                print(f"    evidence {ev_id} EXISTS")
                continue
            close = difflib.get_close_matches(ev_id, known, n=1, cutoff=0.6)
            hint = f" closest={close[0]}" if close else " no-close-match"
            print(f"    evidence {ev_id} MISSING{hint}")
    print(f"=== {label}: applied={applied} rejected={rejected}")
    return result


def main():
    draft, offline = load_segments()
    provider = config.MEETING_LLM_PROVIDER
    model = config.MEETING_LLM_MODEL
    api_key = find_provider_api_key(provider)
    live_state = get_live_state(draft, provider, model, api_key)
    run_consolidation(draft, live_state, provider, model, api_key, "legacy-draft")


if __name__ == "__main__":
    main()
