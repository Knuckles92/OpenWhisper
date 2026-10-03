"""A stand-in for ``opencode acp``: speaks ACP on stdio, calls one MCP tool.

Reads the overlay the driver passes in ``OPENCODE_CONFIG_CONTENT`` (agents'
system prompts, MCP server URL and headers), connects to that server when
the first session opens, and answers every ``session/prompt`` by making the
``tools/call`` in ``FAKE_TOOL_CALL`` with the session id in ``_meta``, as
OpenCode does. Every request it sees is appended to ``FAKE_ACP_LOG``.
"""
import json
import os
import sys
import urllib.request

overlay = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
server = overlay["mcp"]["servers"]["openwhisper"]
log_path = os.environ.get("FAKE_ACP_LOG")
sessions = {}
state = {"connected": False, "next": 1}
MODELS = [{"value": "prov/fast", "name": "prov/Fast"}, {"value": "prov/slow", "name": "prov/Slow"}]


def log(entry):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def post(message):
    request = urllib.request.Request(
        server["url"], data=json.dumps(message).encode(), method="POST",
        headers={"Content-Type": "application/json", **server["headers"]},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
    return json.loads(body) if body else None


def options(session_id):
    current = sessions[session_id]
    return [
        {"id": "model", "type": "select", "currentValue": current["model"], "options": MODELS},
        {"id": "effort", "type": "select", "currentValue": "default",
         "options": [{"value": "default"}, {"value": "low"}]},
        {"id": "mode", "type": "select", "currentValue": current["mode"],
         "options": [{"value": "build"}, {"value": "plan"}]},
    ]


def handle(msg):
    method, params, msg_id = msg.get("method"), msg.get("params") or {}, msg.get("id")
    log({"method": method, "params": params})
    if method == "initialize":
        return {"protocolVersion": 1, "agentInfo": {"name": "Fake", "version": "2.0.0"},
                "agentCapabilities": {"sessionCapabilities": {"delete": {}}}}
    if method == "session/new":
        session_id = f"ses_{state['next']}"
        state["next"] += 1
        sessions[session_id] = {"model": "prov/slow", "mode": "build", "effort": "default"}
        if not state["connected"]:
            post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}})
            post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            state["connected"] = True
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "available_commands_update",
                           "availableCommands": []}}})
        return {"sessionId": session_id, "configOptions": options(session_id)}
    if method == "session/set_config_option":
        sessions[params["sessionId"]][params["configId"]] = params["value"]
        return {"configOptions": options(params["sessionId"])}
    if method == "session/prompt":
        session_id = params["sessionId"]
        call = json.loads(os.environ.get("FAKE_TOOL_CALL") or "null")
        if call:
            call = dict(call, _meta={"ai.opencode/sessionID": session_id})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id, "update": {
                    "sessionUpdate": "tool_call", "title": "openwhisper_" + call["name"]}}})
            post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Done."}}}})
        return {"stopReason": "end_turn",
                "usage": {"inputTokens": 40, "outputTokens": 5, "totalTokens": 45}}
    if method == "session/delete":
        sessions.pop(params.get("sessionId"), None)
        return {}
    if msg_id is not None:
        raise KeyError(method)
    return None


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "method" not in msg:
        continue
    try:
        result = handle(msg)
    except KeyError as exc:
        send({"jsonrpc": "2.0", "id": msg.get("id"),
              "error": {"code": -32601, "message": f"unknown {exc}"}})
        continue
    if "id" in msg:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
