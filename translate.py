"""LLM translation with timeout/retry.

Extracted verbatim from main.py (pre-v1.8.0 split); method `_translate_text`
became module function `translate_text` with explicit dependency injection
(context, config, event).
"""

import asyncio

from astrbot.api import logger

from . import context_utils, prompts, text_utils

TRANSLATION_ERRORS = 0


def _count_translation_error() -> None:
    global TRANSLATION_ERRORS
    TRANSLATION_ERRORS += 1


async def translate_text(context, config, text, event) -> str | None:
    """Translate `text` to the configured voice language via the LLM.

    Returns the cleaned translation, or None on failure/timeout
    (callers fall back to text-only output).
    """
    voice_lang = config.get("voice_language", "Japanese")
    custom_instructions = config.get("translate_instructions", "").strip()

    # Fallback matches the schema default: cue-free, so a config missing the
    # key never starts emitting markup an unknown TTS provider may read aloud.
    emotion_mode = config.get("emotion_intensity", "universal_safe")
    emotion_policy = prompts.emotion_policy_block(emotion_mode, voice_lang)
    emotion_system_line = prompts.emotion_system_line(emotion_mode)

    try:
        persona_block, context_block = context_utils.build_reference_blocks(
            config, event
        )
    except Exception:
        logger.debug(
            "[text_voice_lang_split] Reference context unavailable; "
            "translating without it",
            exc_info=True,
        )
        persona_block, context_block = "", ""
    if context_utils.normalize_context_mode(
        config.get("translate_context_mode", context_utils.DEFAULT_CONTEXT_MODE)
    ) != context_utils.DEFAULT_CONTEXT_MODE and not (persona_block or context_block):
        logger.debug(
            "[text_voice_lang_split] Context mode enabled but no persona or "
            "context captured; using the plain translation prompt"
        )
    has_reference = bool(persona_block or context_block)
    system_prompt = prompts.build_translation_system_prompt(
        emotion_system_line, has_reference
    )

    prompt = prompts.build_translation_prompt(
        voice_lang,
        custom_instructions,
        emotion_policy,
        text,
        persona_block,
        context_block,
    )

    provider_id = config.get("translate_provider", "").strip()
    if not provider_id:
        provider_id = event.get_extra("selected_provider")
    if not provider_id:
        provider_id = await context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )

    timeout = config.get("translate_timeout", 30.0)

    for attempt in range(2):
        try:
            logger.debug(
                f"[text_voice_lang_split] Translating with provider: {provider_id}"
                + (f" (retry {attempt + 1}/2)" if attempt > 0 else "")
            )
            coro = context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            if timeout > 0:
                llm_resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                llm_resp = await coro
            raw = llm_resp.completion_text.strip()
            translated = text_utils.strip_thinking(raw)
            if not translated:
                _count_translation_error()
                return None
            return translated
        except asyncio.TimeoutError:
            if attempt < 1:
                logger.info(
                    f"[text_voice_lang_split] Translation timed out after {timeout}s, "
                    f"retrying after short delay to refresh connection..."
                )
                await asyncio.sleep(0.5)
                continue
            _count_translation_error()
            logger.warning(
                f"[text_voice_lang_split] Translation timed out after {timeout}s "
                f"(retries exhausted), falling back"
            )
            return None
        except Exception:
            _count_translation_error()
            logger.warning(
                "[text_voice_lang_split] Translation failed, falling back",
                exc_info=True,
            )
            return None
