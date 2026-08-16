"""Post-meeting report-view settings and consolidation prompt trimming."""
import re

from meeting.agent.prompts import (
    _CONSOLIDATION_STEPS,
    build_checkpoint_user_prompt,
    build_consolidation_instructions,
)

_STEP_RE = re.compile(r"^(\d+)\. ")


def _numbered_steps(text: str) -> list[str]:
    return [line for line in text.splitlines() if _STEP_RE.match(line)]


def test_all_views_keep_every_step():
    text = build_consolidation_instructions(("ribbon", "brief", "signal"))
    assert "Populate the timeline card" in text
    assert "professional minutes" in text
    assert "Make decisions and action items complete" in text
    steps = _numbered_steps(text)
    assert len(steps) == len(_CONSOLIDATION_STEPS)
    assert steps[0].startswith("1. ")
    assert any(line.startswith("11. ") for line in steps)


def test_brief_only_omits_ribbon_steps():
    text = build_consolidation_instructions(("brief",))
    assert "Populate the timeline card" not in text
    assert "professional minutes" not in text
    assert "notes page from the complete transcript" not in text
    assert "Make decisions and action items complete" in text
    assert "Capture risks, blockers" in text
    steps = _numbered_steps(text)
    assert len(steps) == len(_CONSOLIDATION_STEPS) - 2
    numbers = [int(line.split(".", 1)[0]) for line in steps]
    assert numbers == list(range(1, len(steps) + 1))


def test_empty_views_keep_all_steps():
    text = build_consolidation_instructions(())
    assert "Populate the timeline card" in text
    assert "professional minutes" in text


def test_checkpoint_prompt_reads_report_views():
    prompt = build_checkpoint_user_prompt(
        {
            "participants": {},
            "cards": {},
            "questions": [],
            "report_views": ["brief"],
        },
        [],
        is_consolidation=True,
    )
    assert "Populate the timeline card" not in prompt
    assert "professional minutes" not in prompt
    assert "Make decisions and action items complete" in prompt
