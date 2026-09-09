"""Pi agent using the shared meeting sidecar supervisor."""
from meeting.agent.sidecar import SidecarAgent


class PiSidecarAgent(SidecarAgent):
    """The bundled Node/Pi implementation of AgentCore."""
