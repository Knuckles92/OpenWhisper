"""Snippets: storage, trigger matching, placeholders, and light Markdown."""

import pytest

from config import config
from services.settings import SettingsKey
from services.snippets import (
    Snippet,
    SnippetPlan,
    delete_snippet,
    expand,
    load_snippets,
    markdown_to_html,
    markdown_to_plain,
    normalize,
    plan_expansion,
    prompt_guard,
    save_snippet,
    trigger_note,
    validate_snippet,
)

CAL = Snippet("cal", "my calendar link", "https://cal.example.com/dana?ref=email#slots")
EMAIL = Snippet("email", "my email", "dana@example.com")
EMAIL_ADDRESS = Snippet("address", "my email address", "dana.lee@example.com")
SIGN_OFF = Snippet(
    "sig", "my sign-off",
    "**Dana Lee**\nProduct designer\n[dana.design](https://dana.design)", formatted=True,
)
ALL = [CAL, EMAIL, EMAIL_ADDRESS, SIGN_OFF]


def _stored(*snippets):
    return {SettingsKey.DICTATION_SNIPPETS: [s.to_dict() for s in snippets]}


# --- storage -----------------------------------------------------------------


def test_normalize_ignores_case_punctuation_and_hyphens_but_keeps_words_whole():
    assert normalize("  My  Sign-Off!  ") == "my sign off"
    assert normalize("Don’t, Acme.com") == "don't acme.com"
    assert normalize("?!") == ""


def test_load_keeps_valid_snippets_in_order_and_skips_the_rest():
    settings = {SettingsKey.DICTATION_SNIPPETS: [
        CAL.to_dict(),
        "not a dict",
        {"id": "x", "trigger": "no text"},
        {"id": "y", "trigger": "blank", "text": "   "},
        {"id": "z", "trigger": "a", "text": "too short a trigger"},
        {"id": "w", "trigger": "t" * 61, "text": "too long a trigger"},
        {"id": "v", "trigger": "too long a text", "text": "x" * 10_001},
        {"id": "cal", "trigger": "same id", "text": "duplicate id"},
        {"id": "u", "trigger": "My Calendar, Link!", "text": "duplicate trigger"},
        {"id": 7, "trigger": "bad id", "text": "text"},
        {**EMAIL.to_dict(), "formatted": "yes"},
        SIGN_OFF.to_dict(),
    ]}

    assert load_snippets(settings) == [CAL, EMAIL, SIGN_OFF]
    assert load_snippets({SettingsKey.DICTATION_SNIPPETS: "nope"}) == []
    assert load_snippets({}) == []


def test_load_caps_the_library():
    many = [Snippet(f"id{i}", f"phrase number {i}", "text") for i in range(config.MAX_SNIPPETS + 5)]

    assert len(load_snippets(_stored(*many))) == config.MAX_SNIPPETS


def test_save_adds_replaces_and_delete_removes():
    settings = {}
    save_snippet(settings, CAL)
    save_snippet(settings, EMAIL)
    save_snippet(settings, Snippet("cal", "  my booking link ", "https://cal.example.com/new"))

    assert load_snippets(settings) == [
        Snippet("cal", "my booking link", "https://cal.example.com/new"), EMAIL,
    ]
    assert settings[SettingsKey.DICTATION_SNIPPETS][0] == {
        "id": "cal", "trigger": "my booking link",
        "text": "https://cal.example.com/new", "formatted": False,
    }

    delete_snippet(settings, "cal")
    assert load_snippets(settings) == [EMAIL]


@pytest.mark.parametrize("snippet,message", [
    (Snippet("n", "", "text"), "Add a trigger"),
    (Snippet("n", "!!", "text"), "words you can say"),
    (Snippet("n", "x", "text"), "2 to 60"),
    (Snippet("n", "x" * 61, "text"), "2 to 60"),
    (Snippet("n", "say [[S1]]", "text"), "words you can say"),
    (Snippet("n", "my phrase", "  \n"), "Add the text"),
    (Snippet("n", "my phrase", "x" * 10_001), "10,000"),
    (Snippet("n", "MY CALENDAR-LINK.", "text"), "Another snippet already uses this trigger"),
])
def test_validation_explains_what_to_fix(snippet, message):
    with pytest.raises(ValueError, match=message):
        validate_snippet(snippet, _stored(CAL))


def test_saving_the_same_snippet_again_is_not_a_duplicate():
    validate_snippet(Snippet("cal", "My calendar link", "new text"), _stored(CAL))


def test_a_full_library_refuses_new_snippets_but_still_saves_edits():
    many = [Snippet(f"id{i}", f"phrase number {i}", "text") for i in range(config.MAX_SNIPPETS)]
    settings = _stored(*many)

    with pytest.raises(ValueError, match="Delete one"):
        validate_snippet(Snippet("new", "one more phrase", "text"), settings)
    validate_snippet(Snippet("id3", "phrase number 3", "edited"), settings)


@pytest.mark.parametrize("trigger,exclude,note", [
    ("My Email!", "", "Another snippet already uses this trigger."),
    ("my email", "email", "Also part of “my email address”"),
    ("send my calendar link", "", "Includes “my calendar link”"),
    ("my calender link", "", "Sounds close to “my calendar link”"),
    ("signature", "", "A single word"),
    ("my home address", "", ""),
    ("", "", ""),
])
def test_trigger_note(trigger, exclude, note):
    assert trigger_note(trigger, ALL, exclude_id=exclude).startswith(note)


# --- matching ------------------------------------------------------------------


@pytest.mark.parametrize("text", ["My calendar link.", "my CALENDAR link", " my calendar, link! "])
def test_a_whole_utterance_trigger_ignores_case_and_punctuation(text):
    plan = plan_expansion(text, ALL)

    assert plan.whole == CAL
    assert plan.placeholders == ()
    assert plan.text == text


def test_a_trigger_inside_a_sentence_becomes_a_placeholder():
    plan = plan_expansion("Here's my Calendar link, thanks!", ALL)

    assert plan.text == "Here's [[S1]], thanks!"
    assert plan.placeholders == (("[[S1]]", CAL),)
    assert plan.whole is None


def test_every_occurrence_gets_its_own_placeholder_and_the_longest_trigger_wins():
    plan = plan_expansion("Use my email address, or my email, or my email address", ALL)

    assert plan.text == "Use [[S1]], or [[S2]], or [[S3]]"
    assert [snippet.id for _token, snippet in plan.placeholders] == ["address", "email", "address"]


def test_triggers_match_whole_words_only_and_never_across_a_sentence_end():
    assert plan_expansion("Check my emails today", ALL).placeholders == ()
    assert plan_expansion("That's my calendar. Link it later", ALL).placeholders == ()
    plan = plan_expansion("that was my sign off for today", ALL)
    assert plan.text == "that was [[S1]] for today"


def test_nothing_to_plan_returns_the_text_unchanged():
    assert plan_expansion("hello there", ALL) == SnippetPlan("hello there")
    assert plan_expansion("my calendar link", []) == SnippetPlan("my calendar link")
    assert plan_expansion("", ALL) == SnippetPlan("")


def test_text_that_already_reads_as_a_placeholder_is_left_alone():
    assert plan_expansion("The [[S1]] marker and my email", ALL) == SnippetPlan(
        "The [[S1]] marker and my email"
    )


# --- placeholders and expansion -------------------------------------------------


def test_placeholders_round_trip_through_a_cleanup():
    plan = plan_expansion("send it to my email address and my calendar link", ALL)
    cleaned = "Send it to [[S1]] and [[S2]]."

    plain, html, ok = expand(cleaned, plan)

    assert ok
    assert plain == "Send it to dana.lee@example.com and https://cal.example.com/dana?ref=email#slots."
    assert html == ""


def test_placeholders_a_model_respaced_or_lowercased_still_expand():
    plan = plan_expansion("mail my email please", ALL)

    assert expand("Mail [[ s1 ]], please.", plan) == ("Mail dana@example.com, please.", "", True)


@pytest.mark.parametrize("cleaned", [
    "Mail it, please.",
    "Mail [[S1]] and [[S1]], please.",
    "Mail [[S1]] to [[S2]].",
])
def test_a_lost_duplicated_or_invented_placeholder_is_not_ok(cleaned):
    plan = plan_expansion("mail my email please", ALL)

    assert expand(cleaned, plan) == (cleaned, "", False)


def test_a_whole_snippet_expands_to_its_text():
    assert expand("My calendar link.", plan_expansion("My calendar link.", ALL)) == (
        CAL.text, "", True,
    )


def test_a_formatted_whole_snippet_has_rich_text_and_a_readable_plain_text():
    plain, html, ok = expand("my sign off", plan_expansion("my sign off", ALL))

    assert ok
    assert plain == "Dana Lee\nProduct designer\ndana.design (https://dana.design)"
    assert html == (
        '<b>Dana Lee</b><br>Product designer<br><a href="https://dana.design">dana.design</a>'
    )


def test_text_around_a_formatted_snippet_is_escaped_in_the_html():
    plan = plan_expansion("1 < 2 & my email\nthen my sign off", ALL)

    plain, html, ok = expand(plan.text, plan)

    assert ok
    assert plain.startswith("1 < 2 & dana@example.com\nthen Dana Lee\n")
    assert html.startswith("1 &lt; 2 &amp; dana@example.com<br>then <b>Dana Lee</b><br>")


def test_snippet_text_is_never_scanned_for_placeholders():
    tricky = Snippet("t", "my tricky one", "literal [[S2]] stays")
    plan = plan_expansion("my tricky one and my email", [tricky, EMAIL])

    assert expand(plan.text, plan) == ("literal [[S2]] stays and dana@example.com", "", True)


def test_no_placeholders_means_no_guard():
    assert prompt_guard(SnippetPlan("hello")) == ""
    assert prompt_guard(plan_expansion("my calendar link", ALL)) == ""


def test_the_guard_is_one_sentence_naming_every_placeholder():
    one = prompt_guard(plan_expansion("mail my email", ALL))
    three = prompt_guard(plan_expansion("my email, my email address and my calendar link", ALL))

    assert "[[S1]]" in one and "exactly once" in one and "unchanged" in one
    assert "[[S1]], [[S2]] and [[S3]]" in three
    for guard in (one, three):
        assert guard.endswith(".") and guard.count(". ") == 0


# --- light Markdown --------------------------------------------------------------


def test_markdown_renders_emphasis_links_breaks_and_lists_and_escapes_the_rest():
    source = (
        "Hi *there* & **you** <script>\n"
        "\n"
        "- one\n"
        "* [two](https://two.example.com/?a=1&b=2)\n"
        "3. snake_case_name\n"
        "4) see https://x.example.com/a_b.\n"
        "Bye _for now_"
    )

    assert markdown_to_html(source) == (
        "Hi <i>there</i> &amp; <b>you</b> &lt;script&gt;"
        '<ul><li>one</li><li><a href="https://two.example.com/?a=1&amp;b=2">two</a></li></ul>'
        '<ol start="3"><li>snake_case_name</li>'
        '<li>see <a href="https://x.example.com/a_b">https://x.example.com/a_b</a>.</li></ol>'
        "Bye <i>for now</i>"
    )


@pytest.mark.parametrize("href", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"])
def test_unsafe_links_stay_literal_text(href):
    html = markdown_to_html(f"[click]({href})")

    assert "<a" not in html
    assert "click" in html


def test_www_links_get_https():
    assert markdown_to_html("[site](www.example.com)") == '<a href="https://www.example.com">site</a>'


def test_escaped_markers_stay_literal():
    assert markdown_to_html(r"\*not italic\*") == "*not italic*"
    assert markdown_to_plain(r"\*not italic\*") == "*not italic*"


def test_plain_text_keeps_list_markers_and_drops_emphasis():
    assert markdown_to_plain("**Agenda**\n- *one*\n2. [two](https://two.example.com)") == (
        "Agenda\n- one\n2. two (https://two.example.com)"
    )
