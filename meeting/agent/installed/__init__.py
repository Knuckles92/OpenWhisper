"""Meeting agents that run through a coding agent the user already has.

Claude Code and Codex run headless, one process per pass; OpenCode runs over
the Agent Client Protocol in one process per meeting. Either way the agent's
only tools are OpenWhisper's meeting tools, served by a loopback MCP server
that applies every call under the authority of the pass that made it.
"""
