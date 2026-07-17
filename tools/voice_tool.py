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
        "Send a voice message of your current reply."
        " Call this tool when you believe the user would benefit from hearing "
        "the response spoken aloud in the configured voice language, "
        "such as during casual conversation, emotional support, storytelling, "
        "greetings, or spoken-language teaching. "
        "For content that is primarily code, lists, tables, logs, URLs, "
        "brief confirmations, or information-dense technical data, "
        "you may skip calling this tool to avoid unnecessary voice output."
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
