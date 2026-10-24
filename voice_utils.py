"""Voice send helpers: streaming follow-up and deferred voice.

Extracted verbatim from main.py (pre-v1.8.0 split); methods became module
functions with explicit dependency injection (context, config, event).
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from astrbot.api import logger, sp
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Record
from astrbot.core.star.session_llm_manager import SessionServiceManager

from . import text_utils, translate

TVLS_ENABLED_KEY = "tvls_enabled"

# Voice gate verdicts. Every voice consumer (tool, turn-end service, streaming
# follow-up, inline path) must go through these so the promise a caller makes
# and the check a sender performs can never drift apart again.
VOICE_GATE_OK = "ok"
VOICE_GATE_EMPTY = "empty"
VOICE_GATE_TOO_LONG = "too_long"
VOICE_GATE_DISABLED = "disabled"
VOICE_GATE_NO_PROVIDER = "no_provider"
VOICE_GATE_SESSION_TTS_OFF = "session_tts_off"
VOICE_GATE_TTS_OFF = "tts_off"


@dataclass(frozen=True)
class VoiceGate:
    """Outcome of a voice eligibility check.

    ``ok`` False always carries a ``reason`` and is safe to log verbatim; the
    caller decides the user-facing wording (the tool answers the model, the
    sender logs for the operator).
    """

    ok: bool
    reason: str
    text: str = ""
    length: int = 0
    max_chars: int = 0


def coerce_max_chars(value) -> int:
    """Normalize a configured cap; anything unusable means "no limit" (0)."""
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return 0
    return cap if cap > 0 else 0


def voice_gate_text(text, config, filter_patterns) -> VoiceGate:
    """Decide whether `text` is speakable: filter, then emptiness, then cap.

    Semantics are deliberately identical to the pre-refactor checks in
    main.py/voice_utils.py: the cap compares the UNstripped filtered length,
    and fewer than 2 stripped characters counts as nothing to say.
    """
    filtered = text_utils.filter_text_for_tts(text or "", filter_patterns)
    max_chars = coerce_max_chars(config.get("tts_max_chars", 0))
    length = len(filtered)
    if len(filtered.strip()) < 2:
        return VoiceGate(False, VOICE_GATE_EMPTY, filtered, length, max_chars)
    if max_chars > 0 and length > max_chars:
        return VoiceGate(False, VOICE_GATE_TOO_LONG, filtered, length, max_chars)
    return VoiceGate(True, VOICE_GATE_OK, filtered, length, max_chars)


async def voice_gate_env(context, config, event, tts_provider=None) -> VoiceGate:
    """Decide whether the environment can produce a voice right now.

    Fail-closed: a broken integration check must never lead to a voice that
    the rest of the pipeline cannot justify.
    """
    try:
        if not await is_tvls_enabled(event):
            return VoiceGate(False, VOICE_GATE_DISABLED)
        if tts_provider is None:
            tts_provider = context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            return VoiceGate(False, VOICE_GATE_NO_PROVIDER)
        if not await SessionServiceManager.should_process_tts_request(event):
            return VoiceGate(False, VOICE_GATE_SESSION_TTS_OFF)
        provider_config = context.get_config(event.unified_msg_origin)
        if not provider_config.get("provider_tts_settings", {}).get("enable", False):
            return VoiceGate(False, VOICE_GATE_TTS_OFF)
    except Exception:
        logger.warning(
            "[text_voice_lang_split] Voice availability check failed; treating "
            "the voice as unavailable",
            exc_info=True,
        )
        return VoiceGate(False, VOICE_GATE_NO_PROVIDER)
    return VoiceGate(True, VOICE_GATE_OK)


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
        translate_started = time.monotonic()
        translated = await translate.translate_text(context, config, text, event)
        translate_seconds = time.monotonic() - translate_started
        if not translated:
            logger.warning(
                "[text_voice_lang_split] Voice skipped: translation failed after "
                f"{translate_seconds:.2f}s"
            )
            return None

        if config.get("split_tts_by_sentence", False):
            chunks = text_utils.split_sentences(
                translated, int(config.get("tts_split_max_chars", 200))
            )
        else:
            chunks = [translated]

        paths: list[tuple[str, str]] = []
        tts_started = time.monotonic()
        try:
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
        except BaseException:
            # Cancellation (the hold path bounds this with `asyncio.wait_for`)
            # must not leak the audio already generated.
            for p, _ in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            raise
        # One line per voice: the two costs that decide how long the user waits
        # between the text bubble and the voice message.
        logger.info(
            "[text_voice_lang_split] Voice ready in "
            f"{translate_seconds + (time.monotonic() - tts_started):.2f}s "
            f"(translate={translate_seconds:.2f}s, "
            f"tts={time.monotonic() - tts_started:.2f}s, chunks={len(paths)}, "
            f"chars={len(translated)})"
        )
        return paths


async def send_streaming_follow_up(
    context,
    config,
    event,
    session_key: str,
    streaming_texts: dict,
    filter_patterns,
    semaphore,
    stream_ok: bool = True,
) -> None:
    """Translate + TTS the accumulated streaming text and send as follow-up.

    Called from the patched send_streaming wrapper AFTER the text stream
    finishes. Awaited inside RespondStage, so it completes before the
    pipeline-end temporary file cleanup (safe to track files on the event).

    `stream_ok` reports whether the text stream itself completed. A failed
    stream means the user never received the text, so voicing it would speak
    content the user never saw: log and consume the turn's request instead.
    """
    if not stream_ok:
        logger.warning(
            "[text_voice_lang_split] Text stream failed; skipping the streaming "
            "voice (the user never received this text)"
        )
        event.set_extra("_tvls_deferred_voice_sent", True)
        streaming_texts.pop(session_key, None)
        return

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

    tts_provider = context.get_using_tts_provider(event.unified_msg_origin)
    env = await voice_gate_env(context, config, event, tts_provider)
    if not env.ok:
        logger.debug(f"[text_voice_lang_split] Streaming voice skipped ({env.reason})")
        return

    gate = voice_gate_text(accumulated, config, filter_patterns)
    if not gate.ok:
        logger.warning(
            f"[text_voice_lang_split] Streaming voice skipped "
            f"({gate.reason}: {gate.length}/{gate.max_chars} chars)"
        )
        return

    logger.info(
        f"[text_voice_lang_split] Streaming: translating '{accumulated[:50]}...'"
    )

    voice = await produce_voice_paths(
        context, config, event, gate.text, tts_provider, semaphore
    )
    if voice is None:
        logger.warning(
            "[text_voice_lang_split] Streaming translation/TTS failed; "
            "text delivered without a voice"
        )
        return

    for path, _ in voice:
        event.track_temporary_local_file(path)

    delay = config.get("streaming_follow_up_delay", 1.5)
    await asyncio.sleep(delay)

    try:
        for path, chunk_text in voice:
            chain = MessageChain()
            chain.chain = [Record(file=path, url=path, text=chunk_text)]
            await context.send_message(event.unified_msg_origin, chain)
    except Exception:
        logger.error(
            "[text_voice_lang_split] Failed to send streaming voice follow-up",
            exc_info=True,
        )
        logger.warning(
            "[text_voice_lang_split] Streaming voice was only partially sent; "
            "the request is not marked as delivered"
        )
        return

    event.set_extra("_tvls_deferred_voice_sent", True)
    logger.info("[text_voice_lang_split] Streaming voice sent as follow-up")


async def send_deferred_voice(
    context, config, event, text: str, semaphore, temp_manager: TempFileManager
) -> None:
    """Translate + TTS + send the turn's voice message as an independent follow-up.

    Spawned via asyncio.create_task from the plugin's turn-end service. Runs
    concurrently with the rest of the pipeline — it must NOT rely on
    event.track_temporary_local_file (pipeline-end cleanup races with it);
    temp files are tracked via `temp_manager` and released after send.

    `text` has already passed `voice_gate_text` in the caller; the
    availability checks are repeated here as defence in depth, and every
    failure is logged at WARNING so a requested voice is never lost silently.
    """
    tts_provider = context.get_using_tts_provider(event.unified_msg_origin)
    env = await voice_gate_env(context, config, event, tts_provider)
    if not env.ok:
        logger.warning(
            f"[text_voice_lang_split] Deferred voice NOT sent ({env.reason}); "
            "the reply was delivered without a voice"
        )
        return

    voice = await produce_voice_paths(
        context, config, event, text, tts_provider, semaphore
    )
    if voice is None:
        logger.warning(
            "[text_voice_lang_split] Deferred voice translation/TTS failed; "
            "the reply was delivered without a voice"
        )
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
        logger.warning("[text_voice_lang_split] Deferred voice was only partially sent")
    else:
        logger.info("[text_voice_lang_split] Deferred voice sent as follow-up")
    finally:
        for path, _ in voice:
            temp_manager.release(path)
