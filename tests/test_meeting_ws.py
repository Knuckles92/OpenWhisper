"""
Tests for meeting WebSocket hub: hello snapshot, action -> action_result,
guest authorization on the WS action path, reconnect identity, and hostile
input handling.
"""
import asyncio
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.interfaces import OpResult
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from meeting.web.ws import MAX_MESSAGE_CHARS, WsHub

HOST_TOKEN = "host-ws-token-cccccccccccccccccccc"
GUEST_TOKEN = "guest-ws-token-dddddddddddddddddddd"


def recv_until(ws, msg_type, limit=10):
    """Read messages until one of ``msg_type`` arrives (or fail the test)."""
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == msg_type:
            return message
    raise AssertionError(f"No {msg_type!r} message within {limit} messages")


class FakeRepo:
    def __init__(self):
        self.meeting = {
            "id": "m_ws",
            "title": "WS Meeting",
            "status": "active",
            "started_at": "2026-01-01T12:00:00",
            "host_token": HOST_TOKEN,
            "guest_token": GUEST_TOKEN,
            "state_json": "{}",
        }
        self.segments = [
            {"id": "sg_1", "start_s": 1.0, "end_s": 2.0, "text": "hi",
             "channel": "mic", "speaker_participant_id": None},
        ]
        #: Hook fired inside ``get_last_segments`` to simulate a mutation
        #: landing while the hello snapshot is being assembled.
        self.on_get_last_segments = None

    def get_meeting(self, meeting_id):
        return dict(self.meeting) if meeting_id == self.meeting["id"] else None

    def get_last_segments(self, meeting_id, n):
        if self.on_get_last_segments is not None:
            hook, self.on_get_last_segments = self.on_get_last_segments, None
            hook()
        return list(self.segments)[:n]


class FakeStore:
    def __init__(self):
        self._subs = []
        self._state = {
            "meeting_id": "m_ws",
            "seq": 3,
            "title": "WS Meeting",
            "status": "active",
            "cards": {"key_points": [], "decisions": [], "action_items": [],
                      "risks": [], "timeline": [], "user_notes": []},
            "participants": {
                "p_me": {"id": "p_me", "display_name": "Host", "kind": "me"},
            },
            "questions": [],
            "topic": {"current": "Kickoff", "history": []},
            "rolling_summary": "",
            "cloud_enabled": False,
            "intelligence_online": True,
        }

    def snapshot(self):
        return copy.deepcopy(self._state)

    def subscribe(self, cb):
        self._subs.append(cb)

    def unsubscribe(self, cb):
        if cb in self._subs:
            self._subs.remove(cb)

    def add_participant(self, participant):
        self._state["participants"][participant["id"]] = dict(participant)

    def add_note(self, text):
        """Append a note and fan the applied op out to subscribers."""
        self._state["seq"] += 1
        item = {"id": f"it_{text}", "card": "user_notes", "text": text,
                "status": "edited", "author_type": "user", "author_id": None,
                "pinned": False, "revision": 1, "evidence": [],
                "created_at": "", "updated_at": ""}
        self._state["cards"]["user_notes"].append(item)
        result = OpResult(
            ok=True, op={"op": "add_item"}, target_id=item["id"],
            seq=self._state["seq"],
            effect={"entity": "item", "item": item},
        )
        for cb in list(self._subs):
            cb(self._state["seq"], [result])


_ACTIVITY_TICK = {
    "kind": "thinking",
    "label": "Model is thinking through the transcript…",
    "tool": "",
    "pass_kind": "consolidation",
    "ts": "2026-08-16T12:00:00+00:00",
}


class FakeEngine:
    def __init__(self):
        self.meeting_id = "m_ws"
        self.store = FakeStore()
        self.actions = []
        self.add_guest_calls = 0
        self.activity = []
        self.hub = None

    def recent_agent_activity(self):
        return [dict(record) for record in self.activity]

    def apply_client_action(self, actor_type, actor_id, op):
        self.actions.append((actor_type, actor_id, op))
        return [OpResult(
            ok=True,
            op=op,
            target_id="it_new",
            seq=4,
            effect={"entity": "item", "item": {"id": "it_new", "text": op.get("text")}},
        )]

    def add_guest(self, name):
        self.add_guest_calls += 1
        participant = {"id": f"p_guest{self.add_guest_calls}",
                       "display_name": name, "kind": "guest"}
        self.store.add_participant(participant)
        return participant

    def undo(self, seq, participant_id):
        return []


@pytest.fixture
def ws_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from meeting.web.api import create_app

    engine = FakeEngine()
    repo = FakeRepo()
    hub = WsHub(engine, repo)
    hub.get_guest_url = lambda: "http://127.0.0.1:9/m/" + GUEST_TOKEN
    engine.hub = hub
    app = create_app(engine, repo, hub)
    return TestClient(app), engine, repo


# ---------------------------------------------------------------------------
# Real-store fixture: exercises the actual op authorization rules over WS.
# ---------------------------------------------------------------------------

class AuthzEngine:
    """Minimal engine surface backed by a real ``MeetingStateStore``."""

    def __init__(self):
        self.meeting_id = "m_ws"
        self.store = MeetingStateStore(MeetingState(meeting_id="m_ws"))
        self.store.apply("system", None, [{
            "op": "upsert_participant", "display_name": "Me",
            "kind": "me", "is_provisional": False,
        }])

    def apply_client_action(self, actor_type, actor_id, op):
        return self.store.apply(actor_type, actor_id, [op])

    def undo(self, seq, actor_id):
        return self.store.undo(seq, actor_id)

    def add_guest(self, name):
        results = self.store.apply("system", None, [{
            "op": "upsert_participant", "display_name": name,
            "kind": "guest", "is_provisional": False,
        }])
        return dict(results[0].effect["participant"])


@pytest.fixture
def authz_app():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from meeting.web.api import create_app

    engine = AuthzEngine()
    repo = FakeRepo()
    hub = WsHub(engine, repo)
    app = create_app(engine, repo, hub)
    return TestClient(app), engine


class TestWsHello:
    def test_hello_snapshot_shape(self, ws_app):
        client, engine, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "host"
        assert hello["participant_id"] == "p_me"
        assert hello["seq"] == 3
        assert hello["state"]["title"] == "WS Meeting"
        assert hello["state"]["topic"]["current"] == "Kickoff"
        assert isinstance(hello["segments"], list)
        assert hello["segments"][0]["id"] == "sg_1"
        assert hello["meeting"]["id"] == "m_ws"
        assert hello["meeting"]["title"] == "WS Meeting"

    def test_hello_leaks_no_tokens_beyond_the_guest_link(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            hello = ws.receive_json()
        # The host token is never sent to any client, in any field.
        assert HOST_TOKEN not in str(hello)
        # The guest token appears only inside the shareable guest URL.
        assert hello["urls"]["guest"].endswith(GUEST_TOKEN)
        rest = {key: value for key, value in hello.items() if key != "urls"}
        assert GUEST_TOKEN not in str(rest)

    def test_patch_during_hello_window_is_delivered(self, ws_app):
        """A mutation applied while hello is assembled must still reach the client."""
        client, engine, repo = ws_app
        repo.on_get_last_segments = lambda: engine.store.add_note("LOST")

        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            hello = ws.receive_json()
            patch = ws.receive_json()

        assert hello["type"] == "hello"
        # Not in the snapshot...
        assert hello["state"]["cards"]["user_notes"] == []
        # ...therefore it must arrive as a patch, or the client diverges.
        assert patch["type"] == "patch"
        assert patch["results"][0]["effect"]["item"]["text"] == "LOST"
        assert patch["seq"] == 4


class TestAgentActivityWs:
    """Host-only ``agent_activity`` fan-out and hello snapshot."""

    def test_host_receives_agent_activity_guest_does_not(self, ws_app):
        client, engine, _ = ws_app
        tick = {"type": "agent_activity", **_ACTIVITY_TICK}
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as host_ws:
            recv_until(host_ws, "hello")
            with client.websocket_connect(
                f"/ws?token={GUEST_TOKEN}&name=Sam"
            ) as guest_ws:
                recv_until(guest_ws, "hello")
                # Join is broadcast to every ready socket, including this guest.
                assert recv_until(guest_ws, "presence")["event"] == "joined"
                assert recv_until(host_ws, "presence")["event"] == "joined"
                future = asyncio.run_coroutine_threadsafe(
                    engine.hub.broadcast_json(tick, host_only=True),
                    engine.hub._loop,
                )
                future.result(timeout=2.0)
                host_msg = recv_until(host_ws, "agent_activity")
                assert host_msg["type"] == "agent_activity"
                assert host_msg["kind"] == "thinking"
                assert host_msg["label"] == _ACTIVITY_TICK["label"]
                assert host_msg["tool"] == ""
                assert host_msg["pass_kind"] == "consolidation"
                assert host_msg["ts"] == _ACTIVITY_TICK["ts"]
                assert host_msg["tool"] is not None
                assert host_msg["pass_kind"] is not None
                guest_ws.send_json({"type": "ping"})
                guest_msg = guest_ws.receive_json()
                assert guest_msg["type"] == "pong"

    def test_host_hello_carries_recent_ticks(self, ws_app):
        client, engine, _ = ws_app
        older = dict(_ACTIVITY_TICK)
        newer = {
            "kind": "tool",
            "label": "Updating the dashboard…",
            "tool": "patch_state",
            "pass_kind": "cards",
            "ts": "2026-08-16T12:00:01+00:00",
        }
        engine.activity = [older, newer]
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "host"
        assert hello["agent_activity"] == [older, newer]
        assert "type" not in hello["agent_activity"][0]
        assert hello["agent_activity"][0]["tool"] == ""
        assert hello["agent_activity"][1]["tool"] == "patch_state"

    def test_guest_hello_omits_agent_activity_key(self, ws_app):
        client, engine, _ = ws_app
        engine.activity = [dict(_ACTIVITY_TICK)]
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Sam"
        ) as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "guest"
        assert "agent_activity" not in hello

    def test_host_only_tick_skips_not_ready_host(self):
        """Host-only ticks are ephemeral: not-ready hosts are not buffered."""
        pytest.importorskip("fastapi")
        from meeting.web.ws import WsHub, _Connection

        hub = WsHub(FakeEngine(), FakeRepo())
        conn = _Connection(role="host", name="Host")
        hub._connections[object()] = conn
        asyncio.run(hub.broadcast_json(
            {"type": "agent_activity", **_ACTIVITY_TICK},
            host_only=True,
        ))
        assert list(conn.pending) == []
        assert conn.ready is False


class TestWsActionRoundTrip:
    def test_action_echoes_client_action_id(self, ws_app):
        client, engine, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            ws.send_json({
                "type": "action",
                "client_action_id": "ca_abc123",
                "op": {"op": "add_item", "card": "user_notes", "text": "note"},
            })
            result = ws.receive_json()
        assert result["type"] == "action_result"
        assert result["client_action_id"] == "ca_abc123"
        assert result["results"][0]["ok"] is True
        assert result["results"][0]["seq"] == 4
        assert engine.actions[0][0] == "host"
        assert engine.actions[0][2]["text"] == "note"

    def test_ping_pong(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            ws.receive_json()  # hello
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
        assert pong["type"] == "pong"

    def test_guest_requires_name(self, ws_app):
        client, _, _ = ws_app
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws?token={GUEST_TOKEN}") as ws:
                ws.receive_json()

    def test_guest_with_name_gets_hello(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Sam"
        ) as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "guest"
        assert hello["participant_id"] == "p_guest1"


class TestGuestIdentity:
    def test_reconnect_with_guest_id_reuses_participant(self, ws_app):
        client, engine, _ = ws_app
        seen = set()
        for _ in range(4):
            with client.websocket_connect(
                f"/ws?token={GUEST_TOKEN}&name=Mallory&guest_id=g_stable1"
            ) as ws:
                seen.add(recv_until(ws, "hello")["participant_id"])
        assert seen == {"p_guest1"}
        assert engine.add_guest_calls == 1

    def test_reconnect_without_guest_id_still_joins(self, ws_app):
        client, engine, _ = ws_app
        for _ in range(3):
            with client.websocket_connect(
                f"/ws?token={GUEST_TOKEN}&name=Mallory"
            ) as ws:
                recv_until(ws, "hello")
        assert engine.add_guest_calls == 3

    def test_changed_name_gets_a_new_participant(self, ws_app):
        client, engine, _ = ws_app
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Mallory&guest_id=g_stable1"
        ) as ws:
            first = recv_until(ws, "hello")["participant_id"]
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Trudy&guest_id=g_stable1"
        ) as ws:
            second = recv_until(ws, "hello")["participant_id"]
        assert first != second
        assert engine.add_guest_calls == 2


class TestGuestWsAuthorization:
    """Guests reaching host-only ops through the WS action path."""

    def _guest_action(self, client, op):
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Mallory&guest_id=g_authz"
        ) as ws:
            recv_until(ws, "hello")
            ws.send_json({"type": "action", "client_action_id": "ca_1", "op": op})
            return recv_until(ws, "action_result")

    def test_guest_cannot_set_title(self, authz_app):
        client, engine = authz_app
        result = self._guest_action(client, {"op": "set_title", "text": "Guest rename"})
        assert result["results"][0]["ok"] is False
        assert result["results"][0]["reason"] == "host_only"
        assert engine.store.snapshot()["title"] == ""

    def test_guest_cannot_toggle_cloud(self, authz_app):
        client, engine = authz_app
        result = self._guest_action(client, {"op": "set_cloud_enabled", "enabled": True})
        assert result["results"][0]["ok"] is False
        assert result["results"][0]["reason"] == "host_only"
        assert engine.store.snapshot()["cloud_enabled"] is False

    def test_guest_cannot_undo(self, authz_app):
        client, _ = authz_app
        with client.websocket_connect(
            f"/ws?token={GUEST_TOKEN}&name=Mallory&guest_id=g_authz"
        ) as ws:
            recv_until(ws, "hello")
            ws.send_json({"type": "undo", "client_action_id": "ca_u", "seq": 1})
            result = recv_until(ws, "action_result")
        assert result["results"][0]["ok"] is False
        assert result["results"][0]["reason"] == "host_only"

    def test_guest_can_still_add_a_note(self, authz_app):
        """Control: the authz tests above are not passing vacuously."""
        client, engine = authz_app
        result = self._guest_action(
            client, {"op": "add_item", "card": "user_notes", "text": "Guest note"}
        )
        assert result["results"][0]["ok"] is True
        notes = engine.store.snapshot()["cards"]["user_notes"]
        assert [item["text"] for item in notes] == ["Guest note"]

    def test_host_may_set_title(self, authz_app):
        client, engine = authz_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            recv_until(ws, "hello")
            ws.send_json({"type": "action", "client_action_id": "ca_t",
                          "op": {"op": "set_title", "text": "Host title"}})
            result = recv_until(ws, "action_result")
        assert result["results"][0]["ok"] is True
        assert engine.store.snapshot()["title"] == "Host title"


class TestHostileInput:
    """Malformed frames must be answered, never abort the connection."""

    def test_binary_frame_is_rejected_and_socket_survives(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={GUEST_TOKEN}&name=Mallory") as ws:
            recv_until(ws, "hello")
            ws.send_bytes(b"\x00\x01\x02not json")
            error = recv_until(ws, "error")
            assert error["code"] == "binary_unsupported"
            ws.send_json({"type": "ping"})
            assert recv_until(ws, "pong")["type"] == "pong"

    def test_deeply_nested_json_is_rejected(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={GUEST_TOKEN}&name=Mallory") as ws:
            recv_until(ws, "hello")
            ws.send_text("[" * 20000)
            error = recv_until(ws, "error")
            assert error["code"] == "bad_json"
            ws.send_json({"type": "ping"})
            assert recv_until(ws, "pong")["type"] == "pong"

    def test_oversize_payload_is_rejected_before_parsing(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={GUEST_TOKEN}&name=Mallory") as ws:
            recv_until(ws, "hello")
            ws.send_text("x" * (MAX_MESSAGE_CHARS + 1))
            error = recv_until(ws, "error")
            assert error["code"] == "message_too_large"
            ws.send_json({"type": "ping"})
            assert recv_until(ws, "pong")["type"] == "pong"

    def test_absurd_undo_seq_is_rejected(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            recv_until(ws, "hello")
            # 1e400 parses to float infinity; int() of it raises OverflowError.
            ws.send_text('{"type": "undo", "client_action_id": "ca_x", "seq": 1e400}')
            result = recv_until(ws, "action_result")
            assert result["results"][0]["reason"] == "invalid_seq"
            ws.send_json({"type": "undo", "client_action_id": "ca_y",
                          "seq": 10 ** 40})
            assert recv_until(ws, "action_result")["results"][0]["reason"] == \
                "invalid_seq"
            ws.send_json({"type": "ping"})
            assert recv_until(ws, "pong")["type"] == "pong"

    def test_non_object_and_malformed_op_are_rejected(self, ws_app):
        client, _, _ = ws_app
        with client.websocket_connect(f"/ws?token={HOST_TOKEN}") as ws:
            recv_until(ws, "hello")
            ws.send_text("[1, 2, 3]")
            assert recv_until(ws, "error")["code"] == "bad_message"
            ws.send_json({"type": "action", "client_action_id": "ca_m",
                          "op": "not-a-dict"})
            result = recv_until(ws, "action_result")
            assert result["results"][0]["reason"] == "malformed_op"
            ws.send_json({"type": "nonsense"})
            assert recv_until(ws, "error")["code"] == "unknown_type"
