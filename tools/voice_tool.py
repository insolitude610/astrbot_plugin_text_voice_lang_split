from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from .. import voice_utils

# Shown before the emotion taxonomy. It states the call protocol and treats
# the length limit as guidance, never as a promise: the tool runs before the
# reply exists, so only the turn-end sender can decide whether a voice is
# possible (see voice_utils.voice_gate_text).
_CALL_PROTOCOL = """HOW TO CALL THIS TOOL
1. Write your complete reply first, then call this tool once, as the last
   thing you do in that same message. Never call it before the reply exists
   and never call it twice.
2. The voice speaks the text the user receives during this turn, after the
   plugin's noise filters: fenced and inline code, URLs, markdown emphasis, a
   bracketed aside at the very start of the reply, and the user's configured
   removal patterns.
3. Length is guidance, not a promise. When that text is long (roughly more
   than {cap_clause}) the voice is usually skipped, so prefer not calling this
   tool for long replies.
4. After the tool result, output no further text. The voice is delivered by
   the system as a separate message.
5. The system decides whether a voice can be produced. Never tell the user a
   voice is coming, and never apologise for a voice that was not sent.

"""

# The reference voice and its emotion taxonomy (v1.10.1 wording, unchanged).
_VOICE_TAXONOMY = (
    "Send a voice message of your current reply. Your reference voice is ONE"
    " fixed character: gentle, soft, warm, emotionally intimate, with a light"
    " breathy quality (qi sheng). It cannot switch persona, and it reads the"
    " text the user received this turn rather than one chosen sentence, so call"
    " this tool only when that text as a whole fits the character below, at"
    " most once per reply.\n\n"
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


def build_description(max_chars: int) -> str:
    """Compose the tool description for the effective character limit.

    The number is guidance for the model; enforcement lives in
    `voice_utils.voice_gate_text` at send time, so the description must never
    read like a delivery guarantee.
    """
    cap = voice_utils.coerce_max_chars(max_chars)
    clause = (
        f"{cap} characters (counted after those filters)"
        if cap > 0
        else "the configured limit (there is no explicit character limit right now)"
    )
    return _CALL_PROTOCOL.format(cap_clause=clause) + _VOICE_TAXONOMY


_UNAVAILABLE_TEXT = (
    "Voice is unavailable right now, so no voice will be sent. Do not call this "
    "tool again and do not promise a voice."
)

_REFUSAL_BY_REASON = {
    voice_utils.VOICE_GATE_DISABLED: "Voice is currently disabled for this session, do not call again.",
    voice_utils.VOICE_GATE_NO_PROVIDER: _UNAVAILABLE_TEXT,
    voice_utils.VOICE_GATE_SESSION_TTS_OFF: _UNAVAILABLE_TEXT,
    voice_utils.VOICE_GATE_TTS_OFF: _UNAVAILABLE_TEXT,
}

_ALREADY_REQUESTED = (
    "A voice was already requested for this reply. Do not call this tool again "
    "and do not write anything else."
)

_UNAVAILABLE = (
    "Voice is unavailable right now, so no voice will be sent. Do not promise a "
    "voice and do not write anything else."
)

_QUEUED = "Voice request recorded. Do not write anything else."


@dataclass
class VoiceTool(FunctionTool[AstrAgentContext]):
    plugin: Any | None = None
    name: str = "tvls_send_voice"
    description: str = ""
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    def __post_init__(self) -> None:
        if not self.description:
            self.description = build_description(0)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> str:
        """Record a voice request, or explain truthfully why there is none.

        The tool runs before the reply is complete, so it must NOT decide the
        length question: it only refuses what is already certain (unavailable
        environment, or text already delivered that is already over the cap).
        Everything else is answered as a request, never as a promise.
        """
        event = context.context.event
        if event.get_extra("_tvls_voice_requested", False):
            return _ALREADY_REQUESTED

        plugin = self.plugin
        if plugin is not None:
            try:
                env = await voice_utils.voice_gate_env(
                    plugin.context, plugin.config, event
                )
                if not env.ok:
                    logger.info(
                        f"[text_voice_lang_split] Voice tool refused ({env.reason})"
                    )
                    return _REFUSAL_BY_REASON.get(env.reason, _UNAVAILABLE)

                delivered = plugin.delivered_voice_text(event)
                if delivered:
                    gate = plugin.voice_gate_for_text(delivered)
                    if gate.reason == voice_utils.VOICE_GATE_TOO_LONG:
                        logger.info(
                            "[text_voice_lang_split] Voice tool refused "
                            f"(too_long: {gate.length}/{gate.max_chars} chars)"
                        )
                        return (
                            "Voice will NOT be sent: the text already delivered "
                            f"is {gate.length} characters, over the "
                            f"{gate.max_chars}-character limit. Do not promise a "
                            "voice and do not write anything else."
                        )
            except Exception:
                logger.warning(
                    "[text_voice_lang_split] Voice tool gate failed; refusing",
                    exc_info=True,
                )
                return _UNAVAILABLE

        event.set_extra("_tvls_voice_requested", True)
        logger.info("[text_voice_lang_split] Voice tool recorded a request")
        return _QUEUED
