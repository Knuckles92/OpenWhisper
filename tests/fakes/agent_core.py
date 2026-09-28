"""An ``AgentCore`` stand-in for the headless passes over a stored meeting."""
from meeting.interfaces import AgentResult


class ReplayAgentCore:
    """Replays a fixed op batch through its tools when asked to consolidate.

    ``raises`` makes ``consolidate`` fail with that message instead. A re-run
    has no rolling checkpoints, so ``checkpoint`` fails the test; a subclass
    that also polishes overrides it.
    """

    def __init__(self, ops=None, raises=None):
        self.ops = ops or []
        self.raises = raises
        self.cfg = None
        self.tools = None
        self.payload = None
        self.shutdown_calls = 0
        self.canceled = False

    def initialize(self, cfg, tools):
        self.cfg = cfg
        self.tools = tools

    def checkpoint(self, payload):
        raise AssertionError("re-run must not fire rolling checkpoints")

    def consolidate(self, payload):
        self.payload = payload
        if self.raises:
            raise RuntimeError(self.raises)
        results = self.tools.apply_agent_ops(self.ops)
        return AgentResult(ok=True, op_results=results)

    def cancel(self):
        self.canceled = True

    def is_healthy(self):
        return True

    def shutdown(self):
        self.shutdown_calls += 1
