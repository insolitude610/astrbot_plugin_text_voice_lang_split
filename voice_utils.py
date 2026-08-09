"""Voice send helpers: streaming follow-up and deferred voice.

Extracted verbatim from main.py (pre-v1.8.0 split); methods became module
functions with explicit dependency injection (context, config, event).
"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Record
from astrbot.core.star.session_llm_manager import SessionServiceManager

from . import text_utils, translate


async def send_streaming_follow_up(
    context, config, event, session_key: str, streaming_texts: dict, filter_patterns
) -> None:
    """Translate + TTS the accumulated streaming text and send as follow-up.

    Called from the patched send_streaming wrapper AFTER the text stream
    finishes. Awaited inside RespondStage, so it completes before the
    pipeline-end temporary file cleanup (safe to track files on the event).
    """
    if event.get_extra("action_type") == "live":
        logger.info(
            "[text_voice_lang_split] Agent live mode detected, "
            "skipping plugin TTS to avoid conflict with built-in agent TTS"
        )
        streaming_texts.pop(session_key, None)
        return

    if config.get("enable_llm_voice_tool", False):
        if not event.get_extra("_tvls_voice_requested", False):
            logger.debug(
                "[text_voice_lang_split] Voice tool enabled but LLM did not "
                "request voice, skipping streaming follow-up"
            )
            streaming_texts.pop(session_key, None)
            return

    accumulated = streaming_texts.pop(session_key, None)
    if accumulated is None:
        return

    if not accumulated.strip() or len(accumulated.strip()) < 2:
        return

    tts_provider = context.get_using_tts_provider(event.unified_msg_origin)
    if not tts_provider:
        return

    if not await SessionServiceManager.should_process_tts_request(event):
        logger.debug("[text_voice_lang_split] TTS disabled for session, skip")
        return

    provider_config = context.get_config(event.unified_msg_origin)
    if not provider_config.get("provider_tts_settings", {}).get("enable", False):
        logger.debug("[text_voice_lang_split] TTS globally disabled, skip")
        return

    filtered_text = text_utils.filter_text_for_tts(accumulated, filter_patterns)
    if len(filtered_text.strip()) < 2:
        logger.info(
            "[text_voice_lang_split] Nothing speakable after filtering, skip streaming TTS"
        )
        return

    max_chars = config.get("tts_max_chars", 0)
    if max_chars > 0 and len(filtered_text) > max_chars:
        logger.info(
            f"[text_voice_lang_split] Filtered text ({len(filtered_text)} chars) "
            f"exceeds max ({max_chars}), skip TTS"
        )
        return

    logger.info(
        f"[text_voice_lang_split] Streaming: translating '{accumulated[:50]}...'"
    )

    translated = await translate.translate_text(context, config, filtered_text, event)
    if not translated:
        logger.info("[text_voice_lang_split] Streaming translation failed, text only")
        return

    try:
        audio_path = await tts_provider.get_audio(translated)
        if not audio_path:
            logger.error(
                "[text_voice_lang_split] Streaming TTS returned empty path, skipping"
            )
            return
        event.track_temporary_local_file(audio_path)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Streaming TTS generation failed",
            exc_info=True,
        )
        return

    delay = config.get("streaming_follow_up_delay", 1.5)
    await asyncio.sleep(delay)

    chain = MessageChain()
    chain.chain = [Record(file=audio_path, url=audio_path, text=translated)]
    try:
        await context.send_message(event.unified_msg_origin, chain)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Failed to send streaming voice follow-up",
            exc_info=True,
        )
        return

    logger.info("[text_voice_lang_split] Streaming voice sent as follow-up")


async def send_deferred_voice(context, config, event, text: str) -> None:
    """Translate + TTS + send a deferred voice message as an independent follow-up.

    Spawned via asyncio.create_task from `_maybe_send_deferred_voice`. Runs
    concurrently with the rest of the pipeline — this is the racy path for
    event-tracked temp files (Phase 2 switches it to a plugin-owned manager).
    """
    tts_provider = context.get_using_tts_provider(event.unified_msg_origin)
    if not tts_provider:
        logger.debug("[text_voice_lang_split] No TTS provider for deferred voice, skip")
        return

    if not await SessionServiceManager.should_process_tts_request(event):
        logger.debug(
            "[text_voice_lang_split] TTS disabled for session, skip deferred voice"
        )
        return

    provider_config = context.get_config(event.unified_msg_origin)
    if not provider_config.get("provider_tts_settings", {}).get("enable", False):
        logger.debug(
            "[text_voice_lang_split] TTS globally disabled, skip deferred voice"
        )
        return

    translated = await translate.translate_text(context, config, text, event)
    if not translated:
        logger.info("[text_voice_lang_split] Deferred voice translation failed")
        return

    try:
        audio_path = await tts_provider.get_audio(translated)
        if not audio_path:
            logger.error(
                "[text_voice_lang_split] Deferred voice TTS returned empty path"
            )
            return
        event.track_temporary_local_file(audio_path)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Deferred voice TTS generation failed",
            exc_info=True,
        )
        return

    chain = MessageChain()
    chain.chain = [Record(file=audio_path, url=audio_path, text=translated)]
    try:
        await context.send_message(event.unified_msg_origin, chain)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Failed to send deferred voice",
            exc_info=True,
        )
        return

    logger.info("[text_voice_lang_split] Deferred voice sent as follow-up")
