"""Instrument the existing live agent regression across installed API harnesses."""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from benchmarks.meeting_mode import live_agent_eval
from meeting.agent import openrouter_direct, sidecar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("direct", "direct-json", "pi", "opencode"), required=True)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    logging.disable(logging.CRITICAL)
    # Bound this experiment without editing runtime defaults on disk.
    openrouter_direct._CHECKPOINT_TIMEOUT_S = 60.0
    sidecar._CHECKPOINT_TIMEOUT_S = 60.0
    sidecar._CHECKPOINT_STALL_S = 45.0
    metadata = {"requested_harness": args.harness, "initializations": [], "passes": [], "checkpoint_timeout_s": 60}
    factory = live_agent_eval.create_agent_core

    def instrumented_factory(kind, payload_dir=None):
        agent = factory(kind, payload_dir)
        original_initialize = agent.initialize
        original_checkpoint = agent.checkpoint
        def initialize(config, host):
            begin = time.perf_counter()
            original_initialize(config, host)
            if args.harness == "direct-json":
                agent._json_mode = True
            metadata["initializations"].append({"actual_class": type(agent).__name__, "seconds": time.perf_counter() - begin,
                                                "model": config.model, "provider": config.provider})
        def checkpoint(payload):
            begin = time.perf_counter()
            result = original_checkpoint(payload)
            metadata["passes"].append({"seconds": time.perf_counter() - begin, "notes": payload.is_notes, "polish": payload.is_polish,
                                        "ok": result.ok, "usage": result.usage, "applied": sum(x.ok for x in result.op_results),
                                        "rejections": [x.reason for x in result.op_results if not x.ok]})
            print(json.dumps({"harness": args.harness, "pass": len(metadata["passes"]), **{k: v for k, v in metadata["passes"][-1].items() if k not in ("usage", "rejections")}}), flush=True)
            return result
        agent.initialize = initialize
        agent.checkpoint = checkpoint
        return agent

    live_agent_eval.create_agent_core = instrumented_factory
    sys.argv = [sys.argv[0], "--harness", "direct" if args.harness == "direct-json" else args.harness, "--output", str(args.output)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        status = live_agent_eval.main()
    except Exception as exc:
        metadata["error"] = type(exc).__name__
        status = 1
    metadata["wall_seconds"] = time.perf_counter() - started
    result = json.loads(args.output.read_text()) if args.output.exists() else {}
    result["instrumentation"] = metadata
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
