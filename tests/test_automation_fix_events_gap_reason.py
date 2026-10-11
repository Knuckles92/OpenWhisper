"""A gap says why: a position from before a restart is not a loss, and 0 is the start.

Each app run numbers past the last, so a position from an earlier run can't
match a new event. But it used to read like events this run had dropped, and
so did ``since=0`` on every fresh run; after each restart the channel bridge
told every Claude Code session that messages may have been lost. Now the bus
says ``evicted`` only for events this run no longer holds, ``restarted`` for
a position from another run, and nothing for 0 when this run dropped none.
"""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.automation import catalog, events
from services.automation.events import EventBus
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings
from tests.test_automation_fix_api_sse_order import run_stream
from tests.test_automation_fix_mcp_since import TOKEN, recent_events


def restart(*, buffer_size=500):
    """The last position an earlier run gave out, and the bus of the run after it."""
    earlier = EventBus(seq_base=1_000)
    position = [earlier.publish(catalog.RECORDING_STARTED, {}) for _ in range(3)][-1].seq
    earlier.close()
    return position, EventBus(seq_base=2_000, buffer_size=buffer_size)


def publish(bus, count):
    return [bus.publish(catalog.RECORDING_STARTED, {"job_id": str(n)}) for n in range(count)]


@pytest.fixture
def app_bus():
    """The process-wide bus of a run that started after an earlier one."""
    position, fresh = restart()
    previous = events.set_bus(fresh)
    yield position, fresh
    events.set_bus(previous)
    fresh.close()


def test_since_zero_is_everything_since_this_run_started():
    _, bus = restart()
    assert bus.since(0) == ([], None)
    published = publish(bus, 2)
    assert bus.since(0) == (published, None)


def test_a_position_from_before_a_restart_is_told_apart_from_a_loss():
    position, bus = restart()
    published = publish(bus, 2)
    assert bus.since(position) == (published, catalog.GAP_RESTARTED)
    # It still never matches an event of this run.
    assert all(event.seq > position for event in published)


def test_events_this_run_dropped_are_a_loss_from_any_earlier_position():
    position, bus = restart(buffer_size=2)
    first, *held = publish(bus, 3)
    for start in (position, 0, first.seq - 1):
        assert bus.since(start) == (held, catalog.GAP_EVICTED)
    # A client that already had the dropped one missed nothing.
    assert bus.since(first.seq) == (held, None)


def test_deleted_history_is_a_loss_only_for_what_the_client_had_not_seen():
    _, bus = restart()
    seen, unseen = publish(bus, 2)
    bus.clear()
    assert bus.since(seen.seq) == ([], catalog.GAP_EVICTED)
    assert bus.since(unseen.seq) == ([], None)


def test_a_stream_from_the_start_sends_no_gap(app_bus):
    _, bus = app_bus
    published = publish(bus, 2)
    sent = run_stream(0, after_subscribe=lambda: None)
    assert [message[1] for message in sent] == [catalog.RECORDING_STARTED] * 2
    assert [message[0] for message in sent] == [str(event.seq) for event in published]


def test_a_stream_from_before_a_restart_says_so(app_bus):
    position, bus = app_bus
    published = publish(bus, 2)
    gap, *replayed = run_stream(position, after_subscribe=lambda: None)
    assert gap[1] == catalog.EVENTS_GAP
    assert gap[2]["data"] == {"missed_from_seq": position + 1, "reason": catalog.GAP_RESTARTED}
    assert [message[0] for message in replayed] == [str(event.seq) for event in published]


def test_recent_events_from_zero_reports_no_gap_on_a_fresh_run(tmp_path, app_bus):
    position, bus = app_bus
    published = publish(bus, 2)
    path = tmp_path / "history.db"
    DatabaseManager(str(path)).close()
    app = create_app(path, TOKEN, enabled=lambda: True,
                     settings=InMemorySettings({SettingsKey.MCP_ENABLED: True}))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        page = recent_events(client, since_seq=0)
        earlier = recent_events(client, since_seq=position)
    assert page["gap"] is False
    assert [item["seq"] for item in page["events"]] == [event.seq for event in published]
    # Events the earlier run published after that position are gone.
    assert earlier["gap"] is True
