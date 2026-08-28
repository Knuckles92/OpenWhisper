"""Markdown-to-rich-text conversion for GitHub release bodies."""
from ui_qt.utils.release_notes import render_release_notes_html


class TestRenderReleaseNotes:
    def test_empty_notes_render_nothing(self):
        assert render_release_notes_html("") == ""
        assert render_release_notes_html("   \n\n") == ""

    def test_bullets_become_a_list(self):
        html = render_release_notes_html("- First item\n- Second item\n")
        assert html.count("<li") == 2
        assert "First item" in html
        assert "- First item" not in html

    def test_numbered_items_become_an_ordered_list(self):
        html = render_release_notes_html("1. First\n2. Second\n")
        assert "<ol" in html
        assert html.count("<li") == 2

    def test_heading_marks_are_not_shown(self):
        html = render_release_notes_html("### Highlights\n\nSomething changed.")
        assert "Highlights" in html
        assert "###" not in html

    def test_inline_markup_becomes_tags(self):
        html = render_release_notes_html(
            "An **Active** badge, `gemini-3.7`, and *emphasis*."
        )
        assert "<b>Active</b>" in html
        assert "<code" in html and "gemini-3.7" in html
        assert "<i>emphasis</i>" in html
        assert "**" not in html and "`" not in html

    def test_links_keep_their_text_and_target(self):
        html = render_release_notes_html("See [the notes](https://example.com/x).")
        assert 'href="https://example.com/x"' in html
        assert ">the notes</a>" in html

    def test_checksum_section_is_dropped(self):
        notes = (
            "Real change here.\n\n"
            "### SHA-256\n\n"
            "```\n"
            f"{'e4706b1d' * 8}\n"
            "```\n"
        )
        html = render_release_notes_html(notes)
        assert "Real change here." in html
        assert "SHA-256" not in html
        assert "e4706b1d" not in html

    def test_bare_checksum_lines_are_dropped(self):
        notes = f"Update notes.\n\nOpenWhisper-Setup.exe {'ab' * 32}\n"
        html = render_release_notes_html(notes)
        assert "Update notes." in html
        assert "ab" * 32 not in html

    def test_full_changelog_footer_is_dropped(self):
        notes = "A change.\n\n**Full Changelog**: https://example.com/compare\n"
        html = render_release_notes_html(notes)
        assert "A change." in html
        assert "Full Changelog" not in html

    def test_html_in_notes_is_escaped(self):
        html = render_release_notes_html("Use <script>alert(1)</script> carefully")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_fenced_code_survives_when_it_is_not_a_digest(self):
        html = render_release_notes_html("Run it:\n\n```\ngit pull --ff-only\n```\n")
        assert "<pre" in html
        assert "git pull --ff-only" in html

    def test_paragraph_lines_are_joined(self):
        html = render_release_notes_html("One sentence\nwrapped over lines.\n")
        assert "One sentence wrapped over lines." in html
        assert html.count("<p") == 1
