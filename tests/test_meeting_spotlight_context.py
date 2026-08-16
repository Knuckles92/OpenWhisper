"""
Tests for top spotlight insights selection, deduplication, and prompt context formatting.
"""
from meeting.agent.prompts import (
    render_state_compact,
    select_spotlight_items,
)


def test_select_spotlight_items_ranking_and_deduplication():
    cards = {
        "key_points": [
            {
                "id": "it_1",
                "text": "Launch beta version on Monday",
                "status": "proposed",
                "pinned": False,
                "updated_at": "2026-08-16T10:00:00Z",
            },
            {
                "id": "it_2",
                "text": "Different key takeaway regarding architecture",
                "status": "proposed",
                "pinned": False,
                "updated_at": "2026-08-16T10:05:00Z",
            },
        ],
        "decisions": [
            {
                "id": "it_3",
                "text": "Launch beta version by Monday morning",
                "status": "confirmed",
                "pinned": False,
                "updated_at": "2026-08-16T10:02:00Z",
            },
            {
                "id": "it_4",
                "text": "Adopt SQLite for persistence",
                "status": "confirmed",
                "pinned": True,
                "updated_at": "2026-08-16T10:01:00Z",
            },
        ],
        "action_items": [
            {
                "id": "it_5",
                "text": "Deploy staging server",
                "status": "edited",
                "pinned": False,
                "updated_at": "2026-08-16T10:03:00Z",
            },
        ],
        "live_notes": [
            {
                "id": "it_note",
                "text": "Note taker block",
                "status": "proposed",
                "pinned": True,
                "updated_at": "2026-08-16T10:09:00Z",
            },
        ],
    }

    picks = select_spotlight_items(cards, limit=3)
    assert len(picks) == 3

    # it_4 is pinned (highest rank)
    assert picks[0]["id"] == "it_4"
    assert picks[0]["card"] == "decisions"

    # it_3 and it_1 are duplicate/near-paraphrase texts ("Launch beta version on Monday" vs "Launch beta version by Monday morning").
    # it_3 is confirmed (higher rank than it_1), and from decisions (already used).
    # it_5 is from action_items (touched), so it gets picked.
    # it_2 is from key_points (distinct text).
    ids = [p["id"] for p in picks]
    assert "it_4" in ids
    # The duplicate text of it_1/it_3 should only appear at most once in spotlight
    assert not ("it_1" in ids and "it_3" in ids)


def test_render_state_compact_includes_top_insights():
    state = {
        "title": "Roadmap Sync",
        "topic": {"current": "Q3 Planning"},
        "rolling_summary": "Discussed roadmap and priorities.",
        "participants": {
            "p_me": {"display_name": "Me", "kind": "me", "name_source": "human"},
        },
        "cards": {
            "key_points": [
                {
                    "id": "it_1",
                    "text": "Desktop release is primary focus",
                    "status": "proposed",
                    "revision": 1,
                    "updated_at": "2026-08-16T10:00:00Z",
                },
            ],
            "decisions": [
                {
                    "id": "it_2",
                    "text": "Postpone mobile app until Q4",
                    "status": "confirmed",
                    "revision": 2,
                    "pinned": True,
                    "updated_at": "2026-08-16T10:05:00Z",
                },
            ],
            "action_items": [],
            "risks": [],
            "timeline": [],
            "live_notes": [],
            "user_notes": [],
        },
        "questions": [],
    }

    rendered = render_state_compact(state)
    assert "Top Insights (Dashboard Spotlight - 2 active):" in rendered
    assert "[it_2 confirmed] [decisions] Postpone mobile app until Q4" in rendered
    assert "[it_1 proposed] [key_points] Desktop release is primary focus" in rendered
