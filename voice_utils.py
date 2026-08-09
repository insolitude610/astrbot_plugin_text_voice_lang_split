"""Voice send helpers: streaming follow-up and deferred voice.

Extracted verbatim from main.py (pre-v1.8.0 split); methods became module
functions with explicit dependency injection (context, config, event).
"""

import asyncio
import os
from contextlib import asynccontextmanager

from astrbot.api import logger, sp
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Record
from astrbot.core.star.session_llm_manager import SessionServiceManager

from . import text_utils, translate

TVLS_ENABLED_KEY = "tvls_enabled"


async def is_tvls_enabled(event) -> bool:
    """Per-session voice toggle (default enabled, fail-open on DB errors)."""
    try:
        value = await sp.get_async(
            scope="umo",
            scope_id=event.unified_msg_origin,
            key=TVLS_ENABLED_KEY,
            default=True,
        )
        return True if value is None else bool(value)
    except Exception:
        logger.debug(
            "[text_voice_lang_split] Failed to read tvls_enabled, defaulting to enabled"
        )
        return True


async def set_tvls_enabled(event, enabled: bool) -> bool:
    """Persist the per-session voice toggle; returns False on write failure."""
    try:
        await sp.put_async(
            scope="umo",
            scope_id=event.unified_msg_origin,
            key=TVLS_ENABLED_KEY,
            value=enabled,
        )
        return True
    except Exception:
        logger.warning(
            "[text_voice_lang_split] Failed to persist tvls_enabled",
            exc_info=True,
        )
        return False


class TempFileManager:
    """Plugin-owned temporary audio files (independent of the event lifecycle).

    Background sends (deferred voice) cannot rely on
    event.track_temporary_local_file: the pipeline-end cleanup may run before
    the task finishes, either leaking the file (tracked after cleanup) or
    deleting it before send. The plugin tracks and deletes its own files;
    terminate() calls cleanup_all() as a safety net.
    """

    def __init__(self):
        self._files: set[str] = set()

    def track(self, path: str) -> None:
        self._files.add(path)

    def release(self, path: str) -> None:
        if not path:
            return
        self._files.discard(path)
        try:
            os.remove(path)
        except OSError:
            logger.debug(f"[text_voice_lang_split] Temp file already gone: {path}")

    def cleanup_all(self) -> None:
        for path in list(self._files):
            self.release(path)

    def tracked(self) -> set:
        return set(self._files)


@asynccontextmanager
async def voice_slot(semaphore):
    """Bound the translate+TTS critical section; None means unlimited."""
    if semaphore is None:
        yield
        return
    async with semaphore:
        yield


async def produce_voice_paths(
    context, config, event, text: str, tts_provider, semaphore
) -> list[tuple[str, str]] | None:
    """Translate + optionally split + synthesize; returns [(path, chunk_text)].

    Acquires the semaphore slot exactly once — callers must NOT also acquire.
    On any chunk failure, already-generated files are deleted and None is
    returned (callers fall back to text-only, preserving v1.9.1 behavior).
    """
    async with voice_slot(semaphore):
        translated = await translate.translate_text(context, config, text, event)
        if not translated:
            return None

        if config.get("split_tts_by_sentence", False):
            chunks = text_utils.split_sentences(
                translated, int(config.get("tts_split_max_chars", 200))
            )
        else:
            chunks = [translated]

        paths: list[tuple[str, str]] = []
        for chunk in chunks:
            try:
                path = await tts_provider.get_audio(chunk)
            except Exception:
                logger.error(
                    "[text_voice_lang_split] TTS generation failed, keeping original",
                    exc_info=True,
                )
                path = None
            if not path:
                logger.error(
                    "[text_voice_lang_split] TTS returned empty path, skipping"
                )
                for p, _ in paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                return None
            paths.append((path, chunk))
        return paths


async def send_streaming_follow_up(
    context,
    config,
    event,
    session_key: str,
    streaming_texts: dict,
    filter_patterns,
    semaphore,
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

    if not await is_tvls_enabled(event):
        logger.debug("[text_voice_lang_split] Voice disabled for session, skip")
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

    voice = await produce_voice_paths(
        context, config, event, filtered_text, tts_provider, semaphore
    )
    if voice is None:
        logger.info("[text_voice_lang_split] Streaming translation failed, text only")
        return

    for path, _ in voice:
        event.track_temporary_local_file(path)

    delay = config.get("streaming_follow_up_delay", 1.5)
    await asyncio.sleep(delay)

    for path, chunk_text in voice:
        chain = MessageChain()
        chain.chain = [Record(file=path, url=path, text=chunk_text)]
        try:
            await context.send_message(event.unified_msg_origin, chain)
        except Exception:
            logger.error(
                "[text_voice_lang_split] Failed to send streaming voice follow-up",
                exc_info=True,
            )
            return

    logger.info("[text_voice_lang_split] Streaming voice sent as follow-up")


async def send_deferred_voice(
    context, config, event, text: str, semaphore, temp_manager: TempFileManager
) -> None:
    """Translate + TTS + send a deferred voice message as an independent follow-up.

    Spawned via asyncio.create_task from `_maybe_send_deferred_voice`. Runs
    concurrently with the rest of the pipeline — it must NOT rely on
    event.track_temporary_local_file (pipeline-end cleanup races with it);
    temp files are tracked via `temp_manager` and released after send.
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

    if not await is_tvls_enabled(event):
        logger.debug(
            "[text_voice_lang_split] Voice disabled for session, skip deferred voice"
        )
        return

    voice = await produce_voice_paths(
        context, config, event, text, tts_provider, semaphore
    )
    if voice is None:
        logger.info("[text_voice_lang_split] Deferred voice translation failed")
        return

    for path, _ in voice:
        temp_manager.track(path)

    try:
        for path, chunk_text in voice:
            chain = MessageChain()
            chain.chain = [Record(file=path, url=path, text=chunk_text)]
            await context.send_message(event.unified_msg_origin, chain)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Failed to send deferred voice",
            exc_info=True,
        )
    else:
        logger.info("[text_voice_lang_split] Deferred voice sent as follow-up")
    finally:
        for path, _ in voice:
            temp_manager.release(path)
