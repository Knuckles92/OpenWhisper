"""Source-preserving actions for already classified spoken commands."""
import re

from meeting.corrections import MAX_TERM_CHARS, correct_text


def correction_op(command_row, previous):
    text = command_row.get("text", "").strip()
    patterns = (
        r"\b(?:replace|change|correct)\s+(.+?)\s+(?:with|to)\s+(.+?)(?:\s+please)?[.!?]*$",
        r"\b(?:it is|it's|I said)\s+(.+?),?\s+not\s+(.+?)(?:\s+please)?[.!?]*$",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        first, second = (s.strip(" \"'“”‘’.,!?") for s in match.groups())
        source, target = (first, second) if index == 0 else (second, first)
        if not source or not target or max(len(source), len(target)) > MAX_TERM_CHARS or source.casefold() == target.casefold():
            continue
        evidence = [r["id"] for r in previous if r.get("id") != command_row.get("id")
                    and correct_text(r.get("text", ""), {source.lower(): target}) != r.get("text", "")]
        if not evidence:
            continue
        return {"op": "add_item", "card": "user_notes", "text": f"Spoken correction: “{source}” → “{target}”.",
                "evidence": (evidence[-18:] + [command_row["id"]]),
                "data": {"kind": "term_correction", "selected_text": source, "replacement": target,
                         "source": "voice_command", "command": "fix_transcript"}}
    return None
