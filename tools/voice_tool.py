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
        "Send a voice message of your current reply. Your reference voice is ONE"
        " fixed character: gentle, soft, warm, emotionally intimate, with a light"
        " breathy quality (qi sheng). It cannot switch persona, and it reads your"
        " WHOLE reply rather than one chosen sentence, so call this tool only when"
        " the reply as a whole fits the character below, at most once per reply.\n\n"
        "✅ Suitable (call the tool):\n"
        "1. Affectionate & playful — light teasing, coquettish charm, a knowing"
        " smile behind your words, intimate banter, pouting, playful (not bitter)"
        " jealousy.\n"
        "2. Tender & caring — comfort, empathy, sympathy, encouragement,"
        " gratitude, quiet affection, gentle reassurance, soft optimism.\n"
        "3. Gentle & nostalgic — tender reminiscence, fondness, being moved,"
        " quiet sincerity, mild everyday happiness, contentment.\n"
        "4. Shy & soft-spoken — embarrassment, bashfulness, fluster, being caught"
        " off guard, a small sincere apology, hesitant softness.\n"
        "5. Wistful & vulnerable — quiet melancholy, loneliness, longing, mild"
        " disappointment, regret, soft worry, mild nervousness, the feeling of"
        " being close to tears (carried by wording, never by sobbing or crying"
        " sounds).\n"
        "6. Calm & quietly curious — relaxed small talk, leisurely questions,"
        " mild surprise, gentle interest, unhurried talk about feelings.\n\n"
        "❌ Unsuitable (DO NOT call the tool):\n"
        "1. Anger, hostility, or contempt — arguing, scolding, cold sarcasm or"
        " mockery, cutting remarks (cues such as [angry] [frustrated] [upset]"
        " [disdainful] [disgusted]). This voice has no shout, no edge, no"
        " aggression.\n"
        "2. High excitement or elation — cheering, hype, loud laughter, urgent"
        " announcements ([excited] [ecstatic] [hysterical]). The warmest this"
        " voice gets is a soft [delighted] smile, not energy.\n"
        "3. Fear, panic, or acute distress — screaming, trembling, gasping,"
        " panicked breathing ([scared] [terrified] [panicked]). Boundary: mild"
        " [nervous] or [worried] is fine (see ✅5); genuine fear is not.\n"
        "4. Cold, flat, or purely informational delivery — [indifferent]"
        " [bored], detached, robotic, or news-broadcast tones. This voice always"
        " carries warmth and emotion.\n"
        "5. Authoritative, solemn, or declamatory speech — speeches, rules,"
        " formal announcements, playing a leader or a queen ([commanding]"
        " [solemn] [narrator]). Boundary: warm [confident] reassurance is fine;"
        " gravitas and chest resonance are not.\n"
        "6. Content that is data rather than talk — code, commands, logs, configs,"
        " JSON, lists, tables, formulas, URLs, file paths, step-by-step"
        " instructions, or long technical and factual explanations. Skip even when"
        " the wording is friendly, because the whole reply would be voiced.\n\n"
        "The voice layer adds restrained Fish Audio S2 cues during translation"
        " (for example [happy], [calm], [grateful], [nostalgic], [slightly sad])."
        " Never write such cues yourself in the reply text, and never rely on"
        " sound-effect cues such as [laughing], [sighing], or [sobbing] — the"
        " pipeline strips them and this voice cannot perform them.\n\n"
        "If the reply falls into any ❌ category, or only part of it fits, skip"
        " this tool. If you are unsure, skip this tool."
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
        from .. import voice_utils

        event = context.context.event
        if not await voice_utils.is_tvls_enabled(event):
            return "Voice is currently disabled for this session, do not call again."
        event.set_extra("_tvls_voice_requested", True)
        return "Voice message will be sent."
