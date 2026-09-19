"""User-facing voice command examples, shared by desktop and dashboard."""
from services.settings import resolve_typesafe_voice_command_names


def voice_command_guide():
    names = []
    configured = resolve_typesafe_voice_command_names()
    for name in configured:
        if name == "notetaker" and "note taker" in configured:
            continue
        label = {"note taker": "Note taker",
                 "openwhisper": "OpenWhisper", "open whisper": "OpenWhisper"}.get(name, name.capitalize())
        if label not in names:
            names.append(label)
    if "Assistant" in names:
        names.remove("Assistant")
        names.insert(0, "Assistant")
    primary = names[0]
    return {
        "names": names,
        "primary": primary,
        "examples": [
            {"label": "Take a note", "phrase": f"{primary}, note that the launch is Friday."},
            {"label": "Mark a decision", "phrase": f"{primary}, mark that as a decision."},
            {"label": "Add an action", "phrase": f"{primary}, mark that as an action item."},
            {"label": "Get a recap", "phrase": f"{primary}, recap the decisions so far."},
            {"label": "Change topic", "phrase": f"{primary}, new topic: budget."},
            {"label": "Correct a term", "phrase": f"{primary}, replace Acme with Acme Labs."},
        ],
    }
