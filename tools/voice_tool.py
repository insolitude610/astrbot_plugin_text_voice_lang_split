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
        "Send a voice message of your current reply. Your reference voice is"
        " gentle, soft, warm, emotionally intimate, with a light breathy quality"
        " (qi sheng). Call this tool ONLY when your reply's emotional tone"
        " genuinely matches this voice character:\n\n"
        "✅ Suitable (call the tool):\n"
        "1. Affectionate & playful — light teasing, coquettish charm, warm"
        " giggles, intimate banter, a knowing smile behind your words.\n"
        "2. Gentle & nostalgic — soft warmth, tender reminiscence, heartfelt"
        " comfort, quiet sincerity, mild happiness.\n"
        "3. Melancholic & vulnerable — quiet sadness, loneliness, wistful"
        " longing, soft heartache, subdued weeping.\n\n"
        "❌ Unsuitable (DO NOT call the tool):\n"
        "1. Anger / rage — this voice cannot produce shouting, aggressive"
        " tones, or explosive outbursts.\n"
        "2. High excitement / elation — this voice has only soft giggles,"
        " not loud laughter, cheers, or high-energy enthusiasm.\n"
        "3. Fear / panic / terror — no screaming, panicked breathing,"
        " trembling, or high-pitched alarm.\n"
        "4. Cold / apathetic / purely factual delivery — this voice always"
        " carries warmth and emotion; unsuitable for detached, robotic, or"
        " news-broadcast tones.\n"
        "5. Authoritative / commanding / solemn speech — this voice lacks"
        " gravitas, chest resonance, and a commanding presence. Unsuitable"
        " for a leader, queen, or formal declamation.\n\n"
        "If your reply falls into any ❌ category, skip this tool."
        " If you are unsure, skip this tool."
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
