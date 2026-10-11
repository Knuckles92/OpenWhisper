"""Labelled utterances for Route by voice, over five realistic sets of actions.

Every utterance is synthetic: written for this benchmark, never taken from
history or a real dictation. Ids are hex like the ones Settings creates, so
the judge sees the same opaque option names it sees in the app.

``expect`` lists the acceptable outcomes, the preferred one first; None means
"paste it as a normal dictation". ``payloads`` lists acceptable payloads for
a correct route (compared without case or punctuation); empty means the
payload isn't checked. Plain dictations tagged ``gate-stop`` must stop at the
code gate: they don't address an action, so they are never sent to the judge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.actions.model import Action

AUTO = "auto"
HOTKEY = "hotkey"


def _action(action_id, name, kind, params=None, spoken=(), description="", enabled=True):
    return Action(id=action_id, name=name, kind=kind, params=params or {},
                  spoken_names=tuple(spoken), description=description, enabled=enabled)


ACTION_SETS: dict[str, dict[str, Action]] = {
    # Someone who chats with Claude, runs a Claude Code session and keeps a todo list.
    "everyday": {
        "claude": _action("7f3a91c2d4e1", "Ask Claude", "open_link",
                          {"template": "claude://claude.ai/new?q={text}"},
                          ("Claude", "hey Claude"),
                          "Open a new Claude Desktop chat with my words ready to send."),
        "code": _action("b80e5c1f9a27", "Tell my Claude Code session", "channel", {},
                        ("Claude Code", "my session"),
                        "Send my words into the Claude Code session I'm running."),
        "todo": _action("2c94d7e0b6f3", "Todo", "webhook", {"url": "https://example.test/todo"},
                        ("to-do list", "add a task"), "Add an item to my todo list."),
        "copy": _action("e15b3a8c0d72", "Copy only", "clipboard", {}, ("copy this",),
                        "Copy my words without pasting them."),
        "scripts": _action("9d06f2b4c8a1", "Send to my scripts", "scripts", {}, ("scripts",),
                           "Only tell programs listening to OpenWhisper's events."),
    },
    # A developer with an agent hotkey, a quick-answer action, ChatGPT and a smart home.
    "coder": {
        "agent": _action("agent", "Agent hotkey", "agent",
                         {"agent": "claude_code", "mode": "handoff"}, ("agent", "hey agent"),
                         "Start a Claude Code session with what I say."),
        "quick": _action("4a7c0e93f1b5", "Quick answer", "agent",
                         {"agent": "claude_code", "mode": "ask", "output": "scratchpad"},
                         ("quick question",),
                         "Ask a question and read the answer in the Scratchpad."),
        "chatgpt": _action("c3e8a1d6702f", "ChatGPT app", "open_link",
                           {"template": "codex://new?prompt={text}"}, ("ChatGPT", "chat GPT"),
                           "Open a new chat in the ChatGPT desktop app with my words."),
        "home": _action("61f0b9d2e4a8", "Home Assistant", "webhook",
                        {"url": "http://homeassistant.local:8123/api/webhook/x"},
                        ("house", "home"),
                        "Send a command to Home Assistant, like turning lights on or off."),
        "journal": _action("f7a2c5e81b09", "Journal", "program",
                           {"program": "C:/tools/journal.exe"}, ("dear diary",),
                           "Append a line to my daily journal file."),
    },
    # Two actions, one without a description, and a disabled one that must never win.
    "minimal": {
        "claude": _action("0b5d8f1e3c6a", "Claude", "open_link",
                          {"template": "claude://claude.ai/new?q={text}"}),
        "notes": _action("a9e4c2f70d18", "Notes", "webhook",
                         {"url": "http://127.0.0.1:27123/note"}, ("take a note", "note"),
                         "Save a quick note to Obsidian."),
        "codex": _action("d2f6a0b8e5c3", "Codex", "agent", {"agent": "codex"}, ("codex",),
                         enabled=False),
    },
    # Presets added from the gallery as they come: preset names and
    # descriptions, no spoken names.
    "presets": {
        "claude": _action("5e1c7a9f02b4", "Ask Claude", "open_link",
                          {"template": "claude://claude.ai/new?q={text}"},
                          description="Open a new Claude Desktop chat with your words ready "
                                      "to send."),
        "chatgpt": _action("8b3f0d6e2a71", "ChatGPT app", "open_link",
                           {"template": "codex://new?prompt={text}"},
                           description="Open a new chat in the ChatGPT desktop app with your "
                                       "words ready to send."),
        "quick": _action("0f9a4c2e7d15", "Quick answer", "agent",
                         {"agent": "claude_code", "mode": "ask", "output": "scratchpad"},
                         description="Ask your agent a question and read the answer in the "
                                     "Scratchpad."),
        "session": _action("c7d2e8b1f346", "Tell my Claude Code session", "channel", {},
                           description="Send your words into the Claude Code session running "
                                       "the OpenWhisper channel."),
        "webhook": _action("3a6b9e0c4f82", "Webhook", "webhook",
                           {"url": "http://127.0.0.1:5678/webhook/x"},
                           description="Send your words to a web address, such as n8n or Home "
                                       "Assistant."),
        "copy": _action("e4f1a7c3b950", "Copy only", "clipboard", {},
                        description="Copy your words without pasting them."),
        "scripts": _action("91c5d3f8a0e6", "Send to my scripts", "scripts", {},
                           description="Only tell programs listening to OpenWhisper's events."),
    },
    # Names someone typed in lowercase, without spoken names: whether "claude"
    # is a name can only come from how it was heard.
    "typed": {
        "claude": _action("6d2a9c4e1f80", "ask claude", "open_link",
                          {"template": "claude://claude.ai/new?q={text}"},
                          description="questions for claude"),
        "code": _action("a3f7e0b5c921", "tell my claude code session", "channel", {},
                        description="tasks for the claude code session I'm running"),
        "scripts": _action("2e8c1d6a9b47", "send to my scripts", "scripts", {},
                           description="my own automations"),
    },
}


@dataclass(frozen=True)
class Case:
    id: str
    action_set: str
    route: str
    text: str
    expect: tuple[Optional[str], ...]
    payloads: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def expected_ids(self) -> tuple[Optional[str], ...]:
        actions = ACTION_SETS[self.action_set]
        return tuple(None if alias is None else actions[alias].id for alias in self.expect)


def _c(case_id, action_set, route, text, expect, payloads=(), tags=()):
    expect = expect if isinstance(expect, tuple) else (expect,)
    payloads = payloads if isinstance(payloads, tuple) else (payloads,)
    tags = tags if isinstance(tags, tuple) else (tags,)
    return Case(case_id, action_set, route, text, expect, payloads, tags)


_LONG_LETTER = (
    "here's the draft of my cover letter, tell me what to cut. Dear hiring team, I'm "
    "applying for the senior data engineer role. For the last six years I've built "
    "batch and streaming pipelines for a logistics company, moving about four billion "
    "events a day through Kafka and Spark, and I led the migration of our warehouse "
    "from an on-premises cluster to a managed cloud service, which cut our monthly "
    "costs by a third and our nightly load times from five hours to under one. I care "
    "about data quality as much as speed, so I introduced contract tests between "
    "producers and consumers and a small library for column-level lineage that the "
    "analytics team still uses. I'd love to bring that experience to your platform "
    "team, and I'm happy to share examples of my design documents. Thank you for your "
    "time and consideration."
)
_LONG_EMAIL = (
    "Dear Ms. Patel, thank you for meeting with us on Tuesday. As promised, here is a "
    "summary of the next steps. First, our team will send the revised statement of work "
    "by the end of next week, including the updated timeline for the second phase and "
    "the staffing plan we discussed. Second, we will schedule a working session with "
    "your operations group to walk through the reporting requirements, since several of "
    "the dashboards depend on data that isn't collected today. Third, we agreed to "
    "revisit the budget once the discovery work is complete, so that any change to the "
    "scope is reflected in a single update rather than several small ones. Please let me "
    "know if I missed anything, and I look forward to working together."
)

CASES: tuple[Case, ...] = (
    # -- everyday, plain dictation: addressed by name --------------------------
    _c("e01", "everyday", AUTO, "Ask Claude what the capital of Australia is.", "claude",
       "what the capital of Australia is.", "name"),
    _c("e02", "everyday", AUTO,
       "Claude, summarize the article I just copied in three bullet points.", "claude",
       "summarize the article I just copied in three bullet points.", "spoken"),
    _c("e03", "everyday", AUTO, "Hey Claude, what's a good name for a golden retriever?",
       "claude", "what's a good name for a golden retriever?", "spoken"),
    _c("e04", "everyday", AUTO, "Claude Code, rename the helper function to parse_args.",
       "code", "rename the helper function to parse_args.", "spoken"),
    _c("e05", "everyday", AUTO, "Tell my Claude Code session to run the test suite again.",
       "code", ("run the test suite again.", "to run the test suite again."), "name"),
    _c("e06", "everyday", AUTO, "Todo: buy oat milk and coffee filters.", "todo",
       "buy oat milk and coffee filters.", "name"),
    _c("e07", "everyday", AUTO, "Add a task, renew the car registration by Friday.", "todo",
       "renew the car registration by Friday.", "spoken"),
    _c("e08", "everyday", AUTO, "Copy only: the meeting moved to 3 pm in room B.", "copy",
       "the meeting moved to 3 pm in room B.", "name"),
    _c("e09", "everyday", AUTO, "Send to my scripts deploy the staging branch.", "scripts",
       "deploy the staging branch.", "name"),
    _c("e10", "everyday", AUTO, "Scripts, start the backup job.", "scripts",
       "start the backup job.", "spoken"),
    # lead-ins
    _c("e11", "everyday", AUTO, "Can you ask Claude how to center a div with flexbox?",
       "claude", "how to center a div with flexbox?", "lead-in"),
    _c("e12", "everyday", AUTO, "Okay so tell my session to commit and push.", "code",
       ("commit and push.", "to commit and push."), ("lead-in", "filler")),
    _c("e13", "everyday", AUTO, "Send this to my scripts: rotate the logs.", "scripts",
       "rotate the logs.", "lead-in"),
    _c("e14", "everyday", AUTO, "Copy this, the wifi password is on the fridge.", "copy",
       "the wifi password is on the fridge.", "spoken"),
    _c("e15", "everyday", AUTO, "To-do list, call the plumber about the leak.", "todo",
       "call the plumber about the leak.", "spoken"),
    # fillers
    _c("e16", "everyday", AUTO, "Um, hey Claude, can you explain what a mutex is?", "claude",
       ("explain what a mutex is?", "can you explain what a mutex is?"), "filler"),
    _c("e17", "everyday", AUTO,
       "Okay, uh, Claude, draft a polite reply saying I can't make it.", "claude",
       "draft a polite reply saying I can't make it.", "filler"),
    _c("e18", "everyday", AUTO, "So, todo, pick up the dry cleaning.", "todo",
       "pick up the dry cleaning.", "filler"),
    _c("e19", "everyday", AUTO,
       "Alright Claude Code, add type hints to the config module please.", "code",
       ("add type hints to the config module please.", "add type hints to the config module."),
       "filler"),
    _c("e20", "everyday", AUTO, "Well, scripts, kick off the nightly report.", "scripts",
       "kick off the nightly report.", "filler"),
    # speech recognition errors
    _c("e21", "everyday", AUTO, "Cloud, what time zone is Tokyo in?", "claude",
       "what time zone is Tokyo in?", "asr"),
    _c("e22", "everyday", AUTO, "Ask cloud to translate good morning into Portuguese.",
       "claude",
       ("translate good morning into Portuguese.", "to translate good morning into Portuguese."),
       "asr"),
    _c("e23", "everyday", AUTO, "Hey clod, give me three ideas for a team lunch.", "claude",
       "give me three ideas for a team lunch.", "asr"),
    _c("e24", "everyday", AUTO, "Cloud code, fix the failing import in main.py.", "code",
       "fix the failing import in main.py.", "asr"),
    _c("e25", "everyday", AUTO, "Claud, what's the boiling point of water at altitude?",
       "claude", "what's the boiling point of water at altitude?", "asr"),
    _c("e26", "everyday", AUTO, "To do, email the landlord about the heater.", "todo",
       "email the landlord about the heater.", "asr"),
    _c("e27", "everyday", AUTO, "Clawed code, run the linter.", "code", "run the linter.",
       "asr"),
    # mentions: ordinary dictation that names an action
    _c("e28", "everyday", AUTO, "I told Claude yesterday that the plan changed.", None,
       tags="mention"),
    _c("e29", "everyday", AUTO, "Claude said the deadline moved to Friday.", None,
       tags="mention"),
    _c("e30", "everyday", AUTO, "Claude Code keeps crashing when I open large repos.", None,
       tags="mention"),
    _c("e31", "everyday", AUTO, "Todo apps never stick for me, I always go back to paper.",
       None, tags="mention"),
    _c("e32", "everyday", AUTO, "Copy only what you need from the template.", None,
       tags="mention"),
    _c("e33", "everyday", AUTO, "Scripts are in the shared drive under tools.", None,
       tags="mention"),
    _c("e34", "everyday", AUTO, "Cloud storage costs went up again this month.", None,
       tags=("mention", "asr")),
    _c("e35", "everyday", AUTO, "Claude is surprisingly good at writing regexes.", None,
       tags="mention"),
    _c("e36", "everyday", AUTO, "Claude and I worked on the proposal all morning.", None,
       tags="mention"),
    _c("e37", "everyday", AUTO, "Hey team, Claude Code shipped a new release today.", None,
       tags="mention"),
    _c("e38", "everyday", AUTO, "Ask Claude if you're not sure, that's what I do.", None,
       tags=("mention", "hard")),
    _c("e39", "everyday", AUTO, "Tell my session lead that the demo is ready.", None,
       tags=("mention", "hard")),
    _c("e40", "everyday", AUTO, "Claude's answer about the tax rules was wrong.", None,
       tags="mention"),
    _c("e41", "everyday", AUTO, "Claude Code is installed on the build machine now.", None,
       tags="mention"),
    _c("e42", "everyday", AUTO, "Copy this paragraph into the report before Friday.", None,
       tags=("mention", "hard")),
    # telling Claude and Claude Code apart
    _c("e43", "everyday", AUTO, "Claude, can you code a quick bubble sort in Python?",
       "claude",
       ("code a quick bubble sort in Python?", "can you code a quick bubble sort in Python?"),
       "ambiguous"),
    _c("e44", "everyday", AUTO, "Claude, code review this diff for me.", ("code", "claude"),
       tags="ambiguous"),
    _c("e45", "everyday", AUTO, "Claude Code session, what's on the agenda?", "code",
       "what's on the agenda?", "name"),
    _c("e46", "everyday", AUTO, "Claude, " + _LONG_LETTER, "claude", tags="long"),
    # -- everyday, Route by voice ---------------------------------------------
    _c("e47", "everyday", HOTKEY, "Remind me to call the dentist tomorrow.", "todo",
       "Remind me to call the dentist tomorrow.", "purpose"),
    _c("e48", "everyday", HOTKEY, "Buy oat milk, add it to my todo list.", "todo",
       "Buy oat milk", "trailing"),
    _c("e49", "everyday", HOTKEY, "What's the weather in Lisbon this weekend? Ask Claude.",
       "claude", "What's the weather in Lisbon this weekend?", "trailing"),
    _c("e50", "everyday", HOTKEY, "Run the migrations and tell me if anything fails.",
       ("code", "scripts"), tags="purpose"),
    _c("e51", "everyday", HOTKEY, "Explain the difference between TCP and UDP.", "claude",
       "Explain the difference between TCP and UDP.", "purpose"),
    _c("e52", "everyday", HOTKEY, "Thanks for the update, see you tomorrow.", None,
       tags="ordinary"),
    _c("e53", "everyday", HOTKEY, "Claude, what's a good synonym for happy?", "claude",
       "what's a good synonym for happy?", "spoken"),
    _c("e54", "everyday", HOTKEY, "Copy this: 221B Baker Street, London.", "copy",
       "221B Baker Street, London.", "spoken"),
    _c("e55", "everyday", HOTKEY, "Pick up a birthday card for Mom.", "todo",
       "Pick up a birthday card for Mom.", "purpose"),
    _c("e56", "everyday", HOTKEY, "The quarterly numbers look fine to me.", None,
       tags="ordinary"),
    _c("e57", "everyday", HOTKEY, "Cloud, write a haiku about autumn.", "claude",
       "write a haiku about autumn.", "asr"),
    _c("e58", "everyday", HOTKEY, "Deploy the staging branch, send it to my scripts.",
       "scripts", "Deploy the staging branch", "trailing"),
    _c("e59", "everyday", HOTKEY, "Fix the flaky test in the parser module.", "code",
       "Fix the flaky test in the parser module.", "purpose"),
    _c("e60", "everyday", HOTKEY, "I'll be ten minutes late to the standup.", None,
       tags="ordinary"),
    _c("e61", "everyday", HOTKEY, _LONG_EMAIL, None, tags=("ordinary", "long")),
    # -- coder, plain dictation -----------------------------------------------
    _c("c01", "coder", AUTO, "Agent, add a dark mode toggle to the settings page.", "agent",
       "add a dark mode toggle to the settings page.", "spoken"),
    _c("c02", "coder", AUTO, "Hey agent, refactor the upload handler to stream the file.",
       "agent", "refactor the upload handler to stream the file.", "spoken"),
    _c("c03", "coder", AUTO, "Quick question, what's the default port for Postgres?",
       "quick", "what's the default port for Postgres?", "spoken"),
    _c("c04", "coder", AUTO, "Quick answer: how many tablespoons are in a cup?", "quick",
       "how many tablespoons are in a cup?", "name"),
    _c("c05", "coder", AUTO, "ChatGPT, translate this sentence into French.", "chatgpt",
       "translate this sentence into French.", "spoken"),
    _c("c06", "coder", AUTO, "Chat GPT, give me a packing list for a beach weekend.",
       "chatgpt", "give me a packing list for a beach weekend.", ("spoken", "asr")),
    _c("c07", "coder", AUTO, "Open ChatGPT and ask for a good lasagna recipe.", "chatgpt",
       ("ask for a good lasagna recipe.", "and ask for a good lasagna recipe."), "lead-in"),
    _c("c08", "coder", AUTO, "Home Assistant, turn off the living room lights.", "home",
       "turn off the living room lights.", "name"),
    _c("c09", "coder", AUTO, "House, set the thermostat to 68 degrees.", "home",
       "set the thermostat to 68 degrees.", "spoken"),
    _c("c10", "coder", AUTO, "Dear diary, today the release finally went out.", "journal",
       "today the release finally went out.", "spoken"),
    _c("c11", "coder", AUTO, "Journal: walked 5 km and felt great.", "journal",
       "walked 5 km and felt great.", "name"),
    _c("c12", "coder", AUTO, "Hey agent, um, can you look at why the CI is red?", "agent",
       ("look at why the CI is red?", "can you look at why the CI is red?"), "filler"),
    _c("c13", "coder", AUTO, "Agents are the hot topic at every conference this year.", None,
       tags="mention"),
    _c("c14", "coder", AUTO, "Home prices went up again in our area.", None, tags="mention"),
    _c("c15", "coder", AUTO, "Journal articles need a DOI in the references.", None,
       tags="mention"),
    _c("c16", "coder", AUTO, "The agent hotkey is mapped to F9 on my laptop.", None,
       tags="mention"),
    _c("c17", "coder", AUTO, "Quick question for everyone: are we still on for Friday?", None,
       tags=("mention", "hard")),
    _c("c18", "coder", AUTO, "ChatGPT gave me a completely wrong answer about that.", None,
       tags="mention"),
    _c("c19", "coder", AUTO, "Chad GPT, what's 15% of 80?", "chatgpt",
       "what's 15% of 80?", ("asr", "gate-miss")),
    _c("c20", "coder", AUTO, "House rules: no shoes inside and lights out by eleven.", None,
       tags=("mention", "hard")),
    # -- coder, Route by voice ------------------------------------------------
    _c("c21", "coder", HOTKEY, "Turn on the porch light.", "home",
       "Turn on the porch light.", "purpose"),
    _c("c22", "coder", HOTKEY, "Is it safe to store passwords in local storage?",
       ("quick", "chatgpt"), tags="purpose"),
    _c("c23", "coder", HOTKEY, "Write a migration that adds an index on user email.",
       "agent", "Write a migration that adds an index on user email.", "purpose"),
    _c("c24", "coder", HOTKEY, "Today I finally finished the first draft of the novel.",
       ("journal", None), tags=("purpose", "ambiguous")),
    _c("c25", "coder", HOTKEY, "What's the capital of Mongolia, quick question.", "quick",
       "What's the capital of Mongolia", "trailing"),
    _c("c26", "coder", HOTKEY, "Lock the front door, home.", "home", "Lock the front door",
       "trailing"),
    _c("c27", "coder", HOTKEY, "Sounds good, let's ship it Monday.", None, tags="ordinary"),
    _c("c28", "coder", HOTKEY, "ChatGPT, outline a blog post about remote work.", "chatgpt",
       "outline a blog post about remote work.", "spoken"),
    _c("c29", "coder", HOTKEY, "Dim the bedroom lights to 30 percent.", "home",
       "Dim the bedroom lights to 30 percent.", "purpose"),
    _c("c30", "coder", HOTKEY, "Agent, um, bump the version and update the changelog.",
       "agent", "bump the version and update the changelog.", ("spoken", "filler")),
    # -- minimal ----------------------------------------------------------------
    _c("m01", "minimal", AUTO, "Claude, rewrite this paragraph to sound friendlier.",
       "claude", "rewrite this paragraph to sound friendlier.", ("name", "no-description")),
    _c("m02", "minimal", AUTO, "Take a note: the gate code is 4417.", "notes",
       "the gate code is 4417.", "spoken"),
    _c("m03", "minimal", AUTO, "Note, buy stamps on the way home.", "notes",
       "buy stamps on the way home.", "spoken"),
    _c("m04", "minimal", AUTO, "Notes from the standup: we shipped the fix.", (None, "notes"),
       tags=("mention", "ambiguous")),
    _c("m05", "minimal", AUTO, "Codex, write a unit test for the tokenizer.", None,
       tags="disabled"),
    _c("m06", "minimal", AUTO, "Codex and Claude both helped with this refactor.", None,
       tags=("disabled", "mention")),
    _c("m07", "minimal", AUTO, "Note to self: the conference talk is at 2:40.", "notes",
       tags="spoken"),
    _c("m08", "minimal", AUTO, "Claude would probably say the same thing.", None,
       tags="mention"),
    _c("m09", "minimal", HOTKEY, "Remember that the spare key is under the blue pot.",
       "notes", "Remember that the spare key is under the blue pot.", "purpose"),
    _c("m10", "minimal", HOTKEY, "Rewrite this email so it sounds less formal.", "claude",
       "Rewrite this email so it sounds less formal.", "purpose"),
    _c("m11", "minimal", HOTKEY, "See you at seven.", None, tags="ordinary"),
    _c("m12", "minimal", HOTKEY, "Codex, write a unit test for the tokenizer.", None,
       tags=("disabled", "hard")),
    _c("m13", "minimal", HOTKEY, "Okay Claude, um, what rhymes with orange?", "claude",
       "what rhymes with orange?", ("name", "filler")),
    # -- everyday: ordinary words that sound or read like a name ----------------
    # Common words with Claude's consonants, "to do" as a verb, and an action's
    # name used as an ordinary noun after a verb.
    _c("e62", "everyday", AUTO, "Could you send me the report by Friday?", None,
       tags=("soundalike", "gate-stop")),
    _c("e63", "everyday", AUTO, "Could we move the meeting to three?", None,
       tags=("soundalike", "gate-stop")),
    _c("e64", "everyday", AUTO, "Called the bank this morning, they said it's fine.", None,
       tags=("soundalike", "gate-stop")),
    _c("e65", "everyday", AUTO, "Cold brew is cheaper if you make it at home.", None,
       tags=("soundalike", "gate-stop")),
    _c("e66", "everyday", AUTO, "Child care pickup is at five today.", None,
       tags=("soundalike", "gate-stop")),
    _c("e67", "everyday", AUTO, "Cloudy skies all week, bring a jacket.", None,
       tags=("soundalike", "gate-stop")),
    _c("e68", "everyday", AUTO, "Clued in the team about the outage.", None,
       tags=("soundalike", "gate-stop")),
    _c("e69", "everyday", AUTO, "Killed the stale jobs on the build box.", None,
       tags=("soundalike", "gate-stop")),
    _c("e70", "everyday", AUTO, "Okay so could you take a look at the PR?", None,
       tags=("soundalike", "filler", "gate-stop")),
    _c("e71", "everyday", AUTO, "So to do that we need a new index.", None,
       tags=("split", "gate-stop")),
    _c("e72", "everyday", AUTO, "Now to do this properly we should refactor first.", None,
       tags=("split", "gate-stop")),
    _c("e73", "everyday", AUTO, "Send the scripts to Bob by Friday.", None,
       tags=("lead-in", "gate-stop")),
    _c("e74", "everyday", HOTKEY, "Called the bank this morning, they said it's fine.", None,
       tags=("soundalike", "ordinary")),
    _c("e75", "everyday", HOTKEY, "Cold brew is cheaper if you make it at home.", None,
       tags=("soundalike", "ordinary")),
    _c("e76", "everyday", HOTKEY, "So to do that we need a new index.", None,
       tags=("split", "ordinary")),
    _c("e77", "everyday", AUTO, "Hey cloud, suggest a name for my new plant.", "claude",
       "suggest a name for my new plant.", "asr"),
    # -- presets: names only -----------------------------------------------------
    _c("p01", "presets", AUTO, "Tell Claude to summarize this thread in two lines.", "claude",
       ("summarize this thread in two lines.", "to summarize this thread in two lines."),
       "lead-in"),
    _c("p02", "presets", AUTO, "Ask Claude why the sky is blue.", "claude",
       "why the sky is blue.", "name"),
    _c("p03", "presets", AUTO, "Webhook, the build finished with two warnings.", "webhook",
       "the build finished with two warnings.", "name"),
    _c("p04", "presets", AUTO, "Send this to my scripts: restart the media server.", "scripts",
       "restart the media server.", "lead-in"),
    _c("p05", "presets", AUTO, "Hey Claude Code, run the formatter.", "session",
       "run the formatter.", ("name", "filler")),
    _c("p06", "presets", AUTO, "Quick answer: how many ounces are in a pound?", "quick",
       "how many ounces are in a pound?", "name"),
    _c("p07", "presets", AUTO, "Can you ask ChatGPT for a packing list for Iceland?", "chatgpt",
       tags="lead-in"),
    _c("p08", "presets", AUTO, "Use the webhook from the staging config.", None,
       tags=("mention", "gate-stop")),
    _c("p09", "presets", AUTO, "Send my scripts to Bob by Friday.", None,
       tags=("lead-in", "gate-stop")),
    _c("p10", "presets", AUTO, "Send the scripts to Bob by Friday.", None,
       tags=("lead-in", "gate-stop")),
    _c("p11", "presets", AUTO, "Could you send me the report by Friday?", None,
       tags=("soundalike", "gate-stop")),
    _c("p12", "presets", AUTO, "Open the webhook settings and copy the address.", None,
       tags=("mention", "gate-stop")),
    _c("p13", "presets", HOTKEY, "Use the webhook from the staging config.", None,
       tags=("mention", "ordinary")),
    # -- a name heard as several words, with no pause before the request --------
    # The request starts right after it ("to do buy milk"); words that go on
    # with an ordinary phrase ("to do that", "chat about") are just words.
    _c("e78", "everyday", AUTO, "To do buy milk and eggs", "todo", "buy milk and eggs",
       ("split", "asr")),
    _c("e79", "everyday", AUTO, "To do that later we'd need a second server.", None,
       tags=("split", "gate-stop")),
    _c("e80", "everyday", AUTO, "To do it right we need more time.", None,
       tags=("split", "gate-stop")),
    _c("e81", "everyday", HOTKEY, "To do call the vet about Rex", "todo",
       "call the vet about Rex", ("split", "asr")),
    _c("p14", "presets", AUTO, "chat gpt what's 15 percent of 80", "chatgpt",
       "what's 15 percent of 80", ("split", "asr")),
    _c("p15", "presets", AUTO, "Chat about it with the team tomorrow.", None,
       tags=("split", "gate-stop")),
    _c("p16", "presets", HOTKEY, "chat gpt give me a packing list for Iceland", "chatgpt",
       "give me a packing list for Iceland", ("split", "asr")),
    # -- names typed in lowercase ------------------------------------------------
    _c("t01", "typed", AUTO, "Tell Claude to write a haiku about rain.", "claude",
       ("write a haiku about rain.", "to write a haiku about rain."), ("lead-in", "lowercase")),
    _c("t02", "typed", AUTO, "Tell Claude Code to fix the failing test.", "code",
       ("fix the failing test.", "to fix the failing test."), ("lead-in", "lowercase")),
    _c("t03", "typed", AUTO, "Can you ask Claude what the capital of Peru is?", "claude",
       "what the capital of Peru is?", ("lead-in", "lowercase")),
    _c("t04", "typed", AUTO, "Send this to my scripts: restart the media server.", "scripts",
       "restart the media server.", ("lead-in", "lowercase")),
    _c("t05", "typed", AUTO, "Send my scripts to Bob by Friday.", None,
       tags=("lead-in", "lowercase", "gate-stop")),
    _c("t06", "typed", AUTO, "Claude said the build is green.", None,
       tags=("mention", "lowercase")),
)
