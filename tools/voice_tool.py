from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class VoiceTool(FunctionTool[AstrAgentContext]):
    plugin: Any | None = None
    name: str = "tvls_send_voice"
    description: str = (
        "Send a voice message of your current reply. You MUST call this tool"
        " when your reply carries any of these emotional tones, where voice"
        " is essential to delivery:\n\n"
        "1. Playful & intimate — light teasing, coquettish charm, affectionate"
        " banter, a knowing smile behind your words.\n"
        "2. Gentle & nostalgic — soft warmth, tender reminiscence, heartfelt"
        " comfort, quiet sincerity.\n"
        "3. Deep & melancholic — loneliness, gentle sadness, vulnerability,"
        " wistful longing, quiet heartache.\n\n"
        "Do NOT call this tool for replies whose tone is neutral, factual,"
        " angry, confrontational, cold, or otherwise contrary to the three"
        " emotional dimensions above. If you are unsure, skip this tool."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> str:
        event = context.context.event
        event.set_extra("_tvls_voice_requested", True)
        return "Voice message will be sent."
