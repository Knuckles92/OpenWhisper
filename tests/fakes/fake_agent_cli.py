"""A stand-in for ``claude -p`` / ``codex exec``: calls one MCP tool, then reports.

Run as ``python fake_agent_cli.py <mode> <the driver's argv...>``. It reads
the MCP URL and token from the same arguments the real CLI would, makes the
``tools/call`` in ``FAKE_TOOL_CALL`` (JSON ``{"name", "arguments"}``) over
HTTP, and prints the events that CLI prints. Modes: ``claude``, ``codex``,
``claude-fail`` (a signed-out result), ``hang`` (never finishes).
"""
import json
import os
import sys
import time
import urllib.request


def post(url, token, message):
    request = urllib.request.Request(
        url, data=json.dumps(message).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
    return json.loads(body) if body else None


def call_tool(url, token):
    post(url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18"}})
    post(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    post(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    call = json.loads(os.environ.get("FAKE_TOOL_CALL") or "null")
    if call:
        return post(url, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": call})
    return None


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    sys.stdin.read()  # the prompt
    if mode == "hang":
        emit({"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "openwhisper", "status": "connected"}]})
        time.sleep(60)
        return
    if mode.startswith("claude"):
        with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as handle:
            server = json.load(handle)["mcpServers"]["openwhisper"]
        token = server["headers"]["Authorization"].split(" ", 1)[1]
        emit({"type": "system", "subtype": "init", "tools": [],
              "mcp_servers": [{"name": "openwhisper", "status": "connected"}]})
        if mode == "claude-fail":
            emit({"type": "result", "subtype": "success", "is_error": True,
                  "result": "Not logged in · Please run /login"})
            return
        reply = call_tool(server["url"], token)
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__openwhisper__patch_state", "input": {}}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "Done. " + json.dumps(reply)[:80], "num_turns": 2,
              "total_cost_usd": 0.001,
              "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                        "output_tokens": 20}})
        return
    if mode == "codex":
        overrides = dict(
            args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-c"
        )
        url = overrides["mcp_servers.openwhisper.url"]
        token = os.environ[overrides["mcp_servers.openwhisper.bearer_token_env_var"]]
        emit({"type": "thread.started", "thread_id": "t1"})
        emit({"type": "turn.started"})
        call_tool(url, token)
        emit({"type": "item.completed", "item": {"type": "mcp_tool_call",
                                                 "tool": "patch_state", "status": "completed"}})
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 300,
                                                  "cached_input_tokens": 200,
                                                  "output_tokens": 30}})


if __name__ == "__main__":
    main()
