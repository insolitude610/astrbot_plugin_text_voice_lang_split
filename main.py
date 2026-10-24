import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.session_llm_manager import SessionServiceManager

try:  # WebChat's special default persona; absent on some AstrBot versions.
    from astrbot.core.astr_main_agent_resources import (
        CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT,
    )
except Exception:  # pragma: no cover - older AstrBot
    CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT = ""
    logger.debug(
        "[text_voice_lang_split] CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT unavailable; "
        "the WebChat special default persona will not be injected"
    )

try:  # Needed to release sibling plugins' post-send work for held messages.
    from astrbot.core.pipeline.context import call_event_hook
    from astrbot.core.star.star_handler import EventType
except Exception:  # pragma: no cover - layout moved between versions
    call_event_hook = None
    EventType = None
    logger.debug(
        "[text_voice_lang_split] call_event_hook unavailable; the post-send hook "
        "will not run for messages the plugin ships itself"
    )

from . import context_utils, text_utils, voice_utils
from .tools import VoiceTool
from .tools.voice_tool import build_description

# Only a whole sentence repeated verbatim is collapsed: the boundary must be a
# sentence terminator, so laughter / stutters ("哈哈哈…", "好的好的") are never
# touched.
_SENTENCE_ENDINGS = "。？！?!~…"
_MIN_REPEAT_HALF = 8


def _collapse_verbatim_repeat(text: str) -> str:
    """Turn `X + X` into `X` when X is one sentence repeated verbatim.

    Defensive on every axis: exact halves only, at least
    `_MIN_REPEAT_HALF` characters each, and the last character before the seam
    must end a sentence. Anything else is returned unchanged.
    """
    for _ in range(3):
        size = len(text)
        if size < _MIN_REPEAT_HALF * 2 or size % 2:
            break
        half = size // 2
        head, tail = text[:half], text[half:]
        if head != tail or head[-1] not in _SENTENCE_ENDINGS:
            break
        text = head
    return text


class TextVoiceLangSplit(Star):
    """Translate LLM text before TTS so display and speech use different languages.

    Voice tool mode (`enable_llm_voice_tool`) has exactly one voice owner: the
    turn-end service, which runs after the turn's last message has actually
    been delivered. The tool itself only records a request — it runs before
    the reply exists, so it cannot decide the length question (see
    `voice_utils.voice_gate_text`).
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._streaming_texts: dict[str, str] = {}
        self._filter_patterns: list = []
        self._voice_tool: VoiceTool | None = None
        self._filter_patterns = text_utils.compile_filter_patterns(
            config.get("remove_patterns", [])
        )
        concurrency = int(config.get("tts_concurrency", 2))
        self._tts_semaphore = (
            asyncio.Semaphore(concurrency) if concurrency > 0 else None
        )
        self._bg_tasks: set[asyncio.Task] = set()
        self._terminated: bool = False
        self._persona_api_warned: bool = False
        self._temp_manager = voice_utils.TempFileManager()

    async def initialize(self):
        logger.info("[text_voice_lang_split] Plugin initialized")

        if self._voice_tool is None:
            self._voice_tool = VoiceTool(plugin=self)
        self._voice_tool.active = self.config.get("enable_llm_voice_tool", False)
        max_chars = voice_utils.coerce_max_chars(self.config.get("tts_max_chars", 0))
        # The description is read when AstrBot builds the request, so assigning
        # it here keeps the model's guidance in sync with the live config.
        self._voice_tool.description = build_description(max_chars)
        self.context.add_llm_tools(self._voice_tool)
        if self._voice_tool.active:
            logger.info(
                "[text_voice_lang_split] Voice tool enabled; voice follows the "
                f"turn's delivered text (max_chars={max_chars or 'unlimited'})"
            )
            self._log_message_count_settings()

    def _log_message_count_settings(self) -> None:
        """Log the core knobs that decide how many messages a turn ships.

        One line per boot: whether the whole turn is merged into one message
        depends on these three values together, and the combination is not
        visible anywhere else in the log (this is also the fastest way to tell
        "the merge silently did not happen" from "the merge happened").
        """
        try:
            settings = self.context.get_config().get("provider_settings", {})
        except Exception:
            logger.debug(
                "[text_voice_lang_split] Could not read provider_settings for the "
                "message-count log line",
                exc_info=True,
            )
            return
        logger.info(
            "[text_voice_lang_split] Message count: "
            f"buffer_intermediate_messages={settings.get('buffer_intermediate_messages')}, "
            f"streaming_response={settings.get('streaming_response')}, "
            f"unsupported_streaming_strategy={settings.get('unsupported_streaming_strategy')!r} "
            "(merge needs buffer=true + streaming=false + strategy!='turn_off')"
        )

    # ── turn state ────────────────────────────────────────────────────

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    def _reset_turn_state(self, event: AstrMessageEvent) -> None:
        """Clear per-turn voice state; warn if the previous turn left a request.

        `on_llm_request` fires once per turn, so this is the only safe place to
        forget state: a follow-up turn can reuse the same event object.
        """
        if event.get_extra("_tvls_voice_requested", False) and not event.get_extra(
            "_tvls_deferred_voice_sent", False
        ):
            logger.warning(
                "[text_voice_lang_split] A voice requested in the previous turn "
                "was never delivered (the final message was not sent)"
            )
        for key in (
            "_tvls_decorated",
            "_tvls_pipeline_delivered",
            "_tvls_final_seen",
            "_tvls_voice_requested",
            "_tvls_deferred_voice_sent",
        ):
            event.set_extra(key, None)

    def _record_delivered_text(self, event: AstrMessageEvent) -> None:
        """Accumulate the text of the message that was just shipped.

        Called from `after_message_sent`, where `event.get_result()` is still
        the chain RespondStage delivered (it is cleared right after the hook),
        so sibling plugins' rewrites are already applied. A per-result marker
        makes a duplicated hook firing harmless without relying on `id()`.
        """
        result = event.get_result()
        if result is None or getattr(result, "_tvls_confirmed", False):
            return
        try:
            plain = [
                comp.text
                for comp in result.chain
                if isinstance(comp, Plain) and comp.text and comp.text.strip()
            ]
        except Exception:
            plain = []
        if plain:
            recorded = list(event.get_extra("_tvls_pipeline_delivered", None) or [])
            recorded.append(" ".join(text.strip() for text in plain))
            event.set_extra("_tvls_pipeline_delivered", recorded)
        try:
            result._tvls_confirmed = True
        except Exception:  # pragma: no cover - exotic result object
            logger.debug(
                "[text_voice_lang_split] Could not mark the result as recorded"
            )

    def delivered_voice_text(self, event: AstrMessageEvent) -> str:
        """The text a voice for this turn would speak, from ONE authority.

        Preference order matters: the decorated, shipped chain is what the user
        read in the conversation. `send_message_to_user` text is used only when
        the turn shipped no decorated text at all — concatenating the two would
        voice content the user never received as a single message.
        """
        recorded = event.get_extra("_tvls_pipeline_delivered", None) or []
        if isinstance(recorded, list) and recorded:
            return " ".join(str(t).strip() for t in recorded if str(t).strip())
        sent = (
            event.get_extra("_send_message_to_user_current_session_plain_texts", None)
            or []
        )
        if not isinstance(sent, list):
            return ""
        return " ".join(str(t).strip() for t in sent if str(t).strip()).strip()

    def voice_gate_for_text(self, text: str) -> voice_utils.VoiceGate:
        """Gate helper shared by the tool and the turn-end service."""
        return voice_utils.voice_gate_text(text, self.config, self._filter_patterns)

    async def _suppress_duplicate_tool_text(
        self, event: AstrMessageEvent, result
    ) -> bool:
        """Drop an exact repeat of the delivered reply after the tool call.

        AstrBot delivers every tool-loop step separately, so a model that
        repeats its reply after the tool result posts the same text twice and
        the turn-end voice reads it twice. Only a plain-text chain identical to
        the last delivered text is removed, and only when the voice can
        actually be produced from the already delivered text; media, different
        text and turns without a voice request keep their current behavior.
        """
        chain = result.chain
        if not chain or not all(isinstance(comp, Plain) for comp in chain):
            return False
        current = " ".join(
            comp.text.strip() for comp in chain if comp.text and comp.text.strip()
        )
        delivered = event.get_extra("_tvls_pipeline_delivered", None) or []
        if not current or not isinstance(delivered, list) or not delivered:
            return False
        last = str(delivered[-1]).strip()
        if not last or current != last:
            return False
        env = await voice_utils.voice_gate_env(self.context, self.config, event)
        gate = self.voice_gate_for_text(self.delivered_voice_text(event))
        if not (env.ok and gate.ok):
            logger.info(
                "[text_voice_lang_split] Duplicate post-tool text left in place: "
                f"voice unavailable ({env.reason or gate.reason})"
            )
            return False
        chain.clear()
        result.result_content_type = ResultContentType.GENERAL_RESULT
        result.use_t2i_ = False
        self._streaming_texts.pop(self._get_session_key(event), None)
        await self._maybe_send_turn_voice(event)
        logger.info(
            "[text_voice_lang_split] Suppressed the identical post-tool text "
            "message; the voice is serviced from the delivered text"
        )
        return True

    async def _collapse_duplicate_reply_text(
        self, event: AstrMessageEvent, result
    ) -> bool:
        """Collapse a reply the model emitted twice inside one merged turn.

        The model re-emits its sentence after `tvls_send_voice` returns (this is
        the same behaviour v1.13.1 suppressed across two messages). With
        `buffer_intermediate_messages` both copies ship in ONE message, and a
        sibling decorating plugin that runs before this one
        (`meme_manager`, priority 99999) normalizes adjacent plain segments into
        a single one — so both shapes must be handled:
        adjacent identical segments, and one segment whose text is exactly the
        same sentence twice.

        Same policy as the message-level suppression: tool mode + a live voice
        request, exact text only, and only when the voice can actually be
        produced from what is left — otherwise the duplicate is left alone.
        Never touches media, never reorders, never rewrites remaining text.
        """
        chain = result.chain
        kept: list = []
        planned: list[tuple[object, str]] = []
        dropped = 0
        previous = None
        for comp in chain:
            if isinstance(comp, Plain):
                text = (comp.text or "").strip()
                if text and text == previous:
                    dropped += 1
                    continue
                collapsed = _collapse_verbatim_repeat(text)
                if collapsed != text:
                    planned.append((comp, collapsed))
                previous = collapsed
            else:
                # Strictly adjacent only: any other segment breaks the run.
                previous = None
            kept.append(comp)
        if not dropped and not planned:
            return False

        replaced = {id(comp): collapsed for comp, collapsed in planned}
        candidate = " ".join(
            replaced.get(id(comp), (comp.text or "").strip())
            for comp in kept
            if isinstance(comp, Plain)
            and replaced.get(id(comp), (comp.text or "").strip())
        )
        env = await voice_utils.voice_gate_env(self.context, self.config, event)
        gate = self.voice_gate_for_text(candidate)
        if not (env.ok and gate.ok):
            logger.info(
                "[text_voice_lang_split] Duplicate reply text left in place: "
                f"voice unavailable ({env.reason or gate.reason})"
            )
            return False

        for comp, collapsed in planned:
            comp.text = collapsed
        chain[:] = kept
        logger.info(
            "[text_voice_lang_split] Collapsed duplicate reply text inside the "
            f"merged turn ({dropped} repeated segment(s), "
            f"{len(planned)} verbatim repeat(s))"
        )
        return True

    # ── pipeline hooks ────────────────────────────────────────────────

    @filter.on_decorating_result(priority=999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return

        if not result.is_llm_result():
            return

        if not await voice_utils.is_tvls_enabled(event):
            logger.debug("[text_voice_lang_split] Voice disabled for session, skip")
            return

        if event.get_extra("action_type") == "live":
            return

        tool_mode = bool(self.config.get("enable_llm_voice_tool", False))

        # AstrBot ships every tool-loop step as its own message. A model that
        # repeats the reply after the tool result would post the same text
        # twice (and the turn-end voice would read it twice). Suppress exactly
        # that duplicate: only an all-plain chain identical to the already
        # delivered text, only in tool mode with a voice request, and only when
        # the voice can actually be produced. Media and different text stay.
        if (
            tool_mode
            and event.get_extra("_tvls_decorated", False)
            and event.get_extra("_tvls_final_seen", False)
            and event.get_extra("_tvls_voice_requested", False)
            and await self._suppress_duplicate_tool_text(event, result)
        ):
            return

        # With `buffer_intermediate_messages` the whole tool-loop turn ships as
        # ONE chain, so a model that repeats its sentence across steps lands the
        # same text twice inside the same message — the cross-message
        # suppression below can never see that. Collapse exactly the adjacent
        # identical plain segments (tool mode + requested voice only).
        if tool_mode and event.get_extra("_tvls_voice_requested", False):
            logger.info(
                "[text_voice_lang_split] Reply chain at priority 999: "
                + " | ".join(
                    f"{type(comp).__name__}:{len(getattr(comp, 'text', '') or '')}"
                    for comp in result.chain
                )
            )
            await self._collapse_duplicate_reply_text(event, result)

        # The tool-loop delivers the assistant text of every step separately.
        # Only the first one carries the reply; later ones are handled by the
        # turn-end service. Neither may keep the built-in TTS enabled.
        if event.get_extra("_tvls_decorated", False):
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            logger.debug("[text_voice_lang_split] No TTS provider configured, skip")
            return

        if not await SessionServiceManager.should_process_tts_request(event):
            logger.debug("[text_voice_lang_split] TTS disabled for session, skip")
            return

        provider_config = self.context.get_config(event.unified_msg_origin)
        if not provider_config.get("provider_tts_settings", {}).get("enable", False):
            logger.debug("[text_voice_lang_split] TTS globally disabled, skip")
            return

        plain_texts: list[tuple[int, Plain]] = []
        for i, comp in enumerate(result.chain):
            if isinstance(comp, Plain) and comp.text.strip():
                plain_texts.append((i, comp))

        if not plain_texts:
            return

        event.set_extra("_tvls_decorated", True)

        full_text = "".join(comp.text for _, comp in plain_texts)
        if len(full_text.strip()) < 2:
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            return

        gate = voice_utils.voice_gate_text(
            full_text, self.config, self._filter_patterns
        )
        if not gate.ok:
            logger.info(
                "[text_voice_lang_split] No voice for this message "
                f"({gate.reason}: {gate.length}/{gate.max_chars} chars)"
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        if tool_mode:
            # GENERAL_RESULT blocks the built-in TTS, so the plugin stays the
            # only source of audio for this message.
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            if event.get_extra("_tvls_voice_requested", False) and event.get_extra(
                "_tvls_final_seen", False
            ):
                # The turn's last message and the voice it asks for: wait for
                # the audio, then ship "text, voice" as one pair.
                if await self._ship_turn_with_voice(event, result, gate.text):
                    return
            logger.debug(
                "[text_voice_lang_split] Voice tool mode: delivering text only; "
                "the voice is serviced at turn end"
            )
            return

        logger.info(f"[text_voice_lang_split] Translating: '{full_text[:50]}...'")

        voice = await voice_utils.produce_voice_paths(
            self.context,
            self.config,
            event,
            gate.text,
            tts_provider,
            self._tts_semaphore,
        )
        if voice is None:
            self._streaming_texts.pop(self._get_session_key(event), None)
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            logger.info("[text_voice_lang_split] Translation failed, text only")
            return

        for path, _ in voice:
            event.track_temporary_local_file(path)
        for path, chunk_text in voice:
            result.chain.append(Record(file=path, url=path, text=chunk_text))
        result.result_content_type = ResultContentType.GENERAL_RESULT
        result.use_t2i_ = False
        self._streaming_texts.pop(self._get_session_key(event), None)

        logger.info("[text_voice_lang_split] Voice appended to result chain")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        self._reset_turn_state(event)
        # Capture before the streaming-patch guard: the guard returns early on
        # later calls, and the snapshot must exist for every voice path.
        await self._capture_translation_context(event, req)
        if getattr(event, "_tvls_stream_patched", False):
            return
        original = event.send_streaming

        async def _patched(stream, *args, **kwargs):
            stream_ok = False
            try:
                await original(stream, *args, **kwargs)
                stream_ok = True
            finally:
                await voice_utils.send_streaming_follow_up(
                    self.context,
                    self.config,
                    event,
                    event.unified_msg_origin,
                    self._streaming_texts,
                    self._filter_patterns,
                    self._tts_semaphore,
                    stream_ok=stream_ok,
                )

        event.send_streaming = _patched
        event._tvls_stream_patched = True

    @filter.on_llm_response(priority=-1000)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # -1000 so this observes text after sibling handlers (e.g. thinking-tag
        # stripping plugins) have rewritten the response.
        text = resp.completion_text
        if not text and resp.result_chain:
            text = resp.result_chain.get_plain_text()
        text = text or ""

        session_key = self._get_session_key(event)
        self._streaming_texts[session_key] = text

        if not self.config.get("enable_llm_voice_tool", False):
            return

        # `on_llm_response` fires once, when the agent produced its final
        # response. From here on, the next delivered message is the turn's last
        # one — which is where the voice is serviced. A final response with no
        # text will never be delivered, so service immediately.
        event.set_extra("_tvls_final_seen", True)
        if not text.strip():
            await self._maybe_send_turn_voice(event)

    @filter.after_message_sent(priority=999)
    async def after_message_sent(self, event: AstrMessageEvent):
        self._record_delivered_text(event)
        self._streaming_texts.pop(self._get_session_key(event), None)
        if event.get_extra("_tvls_final_seen", False):
            await self._maybe_send_turn_voice(event)

    async def _ship_turn_with_voice(
        self, event: AstrMessageEvent, result, text: str
    ) -> bool:
        """Hold the turn's last message until the audio exists, then ship both.

        Translation + TTS take a few seconds, so shipping the text first makes
        the user watch "text … long gap … voice". Holding the text for that long
        turns it into "text, voice" instead: same total wait, far better flow.

        Returns True only when this method sent the pair itself; the caller then
        returns with an empty result chain so the pipeline sends nothing. Every
        other outcome (disabled, timeout, failure, no voice) returns False and
        leaves the default flow untouched — the text must never be lost.
        """
        try:
            hold_timeout = float(
                self.config.get("hold_text_for_voice_timeout", 20.0) or 0
            )
        except (TypeError, ValueError):
            hold_timeout = 0.0
        if hold_timeout <= 0 or self._terminated:
            return False
        if event.get_extra("_tvls_deferred_voice_sent", False):
            return False
        if event.get_extra("_tvls_pipeline_delivered", None):
            # Only a turn whose whole text is this one message can be held. If
            # anything was already shipped, the user has seen part of the reply
            # and holding the rest would just look broken.
            return False
        if not text:
            return False

        env = await voice_utils.voice_gate_env(self.context, self.config, event)
        if not env.ok:
            logger.info(
                "[text_voice_lang_split] Text shipped immediately: nothing to wait "
                f"for ({env.reason})"
            )
            return False
        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            return False

        try:
            voice = await asyncio.wait_for(
                voice_utils.produce_voice_paths(
                    self.context,
                    self.config,
                    event,
                    text,
                    tts_provider,
                    self._tts_semaphore,
                ),
                timeout=hold_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[text_voice_lang_split] Voice not ready within "
                f"{hold_timeout:g}s; shipping the text now. The turn-end service "
                "will generate the voice again, so this turn costs one extra "
                "translate + TTS call (raise hold_text_for_voice_timeout if it "
                "keeps happening)"
            )
            return False

        if not voice:
            # Translation/TTS failed. Never retry from the turn-end service (the
            # second attempt would fail the same way) but never lose the reply.
            event.set_extra("_tvls_deferred_voice_sent", True)
            logger.warning(
                "[text_voice_lang_split] Voice generation failed while holding the "
                "text; the reply ships without a voice"
            )
            return False

        for path, _ in voice:
            self._temp_manager.track(path)
        text_chain = MessageChain(chain=list(result.chain))

        # Send the text BEFORE touching the result chain: if this fails the
        # reply must fall back to the pipeline instead of disappearing.
        try:
            text_sent = await self.context.send_message(
                event.unified_msg_origin, text_chain
            )
        except Exception:
            text_sent = False
            logger.error(
                "[text_voice_lang_split] Failed to send the held reply text",
                exc_info=True,
            )
        if text_sent is False:
            for path, _ in voice:
                self._temp_manager.release(path)
            logger.warning(
                "[text_voice_lang_split] Could not ship the held reply; handing it "
                "back to the pipeline so the text is never lost (the voice follows "
                "as usual)"
            )
            return False

        # The text is out: from here the pipeline must not send it again.
        result.chain.clear()
        event.set_extra("_tvls_deferred_voice_sent", True)
        logger.info(
            "[text_voice_lang_split] Shipped the held reply text "
            f"({len(text)} chars): '{text[:60]}'"
        )

        sent_chunks = 0
        try:
            for path, chunk_text in voice:
                chain = MessageChain()
                chain.chain = [Record(file=path, url=path, text=chunk_text)]
                await self.context.send_message(event.unified_msg_origin, chain)
                sent_chunks += 1
        except Exception:
            logger.error(
                "[text_voice_lang_split] Failed to send the held voice",
                exc_info=True,
            )
            logger.warning(
                "[text_voice_lang_split] Held voice was only partially sent "
                f"({sent_chunks}/{len(voice)} chunk(s))"
            )
        finally:
            for path, _ in voice:
                self._temp_manager.release(path)

        # The pipeline will never fire this for a message it did not send, but
        # sibling plugins queue work for it (meme_manager flushes its images and
        # deletes its temp files there), so releasing the pair must run it.
        await self._notify_after_message_sent(event)
        logger.info(
            "[text_voice_lang_split] Shipped the turn's text and voice back-to-back "
            f"({sent_chunks}/{len(voice)} voice chunk(s), held {len(text)} chars)"
        )
        return True

    async def _notify_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Run the core post-send hook for the message this plugin shipped.

        Only needed by the held path: the plugin sends that message itself, so
        `RespondStage` skips it and never fires `OnAfterMessageSentEvent`. The
        result chain is already empty by the time this runs, so the hook cannot
        cause a duplicate send of our text.
        """
        if call_event_hook is None or EventType is None:  # pragma: no cover
            logger.debug(
                "[text_voice_lang_split] Post-send hook unavailable on this "
                "AstrBot version; sibling plugins will miss it for held turns"
            )
            return
        try:
            await call_event_hook(event, EventType.OnAfterMessageSentEvent)
        except Exception:
            logger.warning(
                "[text_voice_lang_split] Post-send hook failed for the held message",
                exc_info=True,
            )

    async def _maybe_send_turn_voice(self, event: AstrMessageEvent) -> None:
        """Send the turn's voice once, after the turn's text has been delivered.

        Every exit either spawns a voice or logs a WARNING with the reason, so
        a requested voice can never disappear silently.

        The service requires a DECORATED delivery. With
        `provider_settings.buffer_intermediate_messages` AstrBot ships the whole
        tool-loop turn as one message at the very end, so the final response can
        be produced while nothing has been delivered yet — and
        `on_llm_response` services immediately when that response is empty.
        Treating that window as terminal would reserve the single attempt and
        then skip the merged message, losing the voice; answering it from the
        `send_message_to_user` fallback would voice text the user never received
        as the reply. Neither is acceptable, so the request stays pending until
        a decorated delivery exists.
        """
        if not self.config.get("enable_llm_voice_tool", False):
            return
        if event.get_extra("_tvls_deferred_voice_sent", False):
            return
        if not event.get_extra("_tvls_voice_requested", False):
            return

        delivered = event.get_extra("_tvls_pipeline_delivered", None) or []
        text = self.delivered_voice_text(event)
        if not text or not delivered:
            logger.warning(
                "[text_voice_lang_split] Voice requested but no decorated reply "
                "has been delivered yet; keeping the request pending"
            )
            return

        # Reserve the single attempt only now: the pending window above must
        # stay retryable, a refusal below must stay consumed.
        event.set_extra("_tvls_deferred_voice_sent", True)

        try:
            env = await voice_utils.voice_gate_env(self.context, self.config, event)
            if not env.ok:
                logger.warning(
                    "[text_voice_lang_split] Voice requested but not sent "
                    f"({env.reason})"
                )
                return
            gate = self.voice_gate_for_text(text)
            if not gate.ok:
                logger.warning(
                    "[text_voice_lang_split] Voice requested but not sent "
                    f"({gate.reason}: {gate.length}/{gate.max_chars} chars)"
                )
                return
            # One line per serviced turn: makes the voiced text observable, so
            # "the voice read the merged turn" can be asserted from the log.
            logger.info(
                "[text_voice_lang_split] Voicing the turn's delivered text "
                f"({len(gate.text)} chars): '{gate.text[:50]}'"
            )
            self._spawn_voice_task(
                voice_utils.send_deferred_voice(
                    self.context,
                    self.config,
                    event,
                    gate.text,
                    self._tts_semaphore,
                    self._temp_manager,
                )
            )
        except Exception:
            logger.warning(
                "[text_voice_lang_split] Voice service failed", exc_info=True
            )

    def _spawn_voice_task(self, coro) -> asyncio.Task | None:
        """Register a background voice task; refuse after termination begins."""
        if self._terminated:
            logger.warning(
                "[text_voice_lang_split] Plugin terminating, voice request dropped"
            )
            return None
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "tvls", alias={"语音开关"}, desc="切换本会话的文本-语音分离（管理员）"
    )
    async def tvls_command(self, event: AstrMessageEvent, action: str = "status"):
        cmd = (action or "status").strip().lower()
        if cmd in ("on", "enable", "开"):
            if not await voice_utils.set_tvls_enabled(event, True):
                return event.plain_result("语音开关写入失败，请检查日志。")
            return event.plain_result("本会话文本-语音分离已开启。")
        if cmd in ("off", "disable", "关"):
            if not await voice_utils.set_tvls_enabled(event, False):
                return event.plain_result("语音开关写入失败，请检查日志。")
            return event.plain_result("本会话文本-语音分离已关闭。")
        enabled = await voice_utils.is_tvls_enabled(event)
        return event.plain_result(
            f"本会话文本-语音分离状态：{'开启' if enabled else '关闭'}\n"
            f"用法：/tvls on|off（管理员）"
        )

    async def _capture_translation_context(self, event, req) -> None:
        """Snapshot the persona and pre-turn history for the translation prompt.

        Runs inside `on_llm_request`, i.e. after the pipeline resolved the
        conversation and applied the persona, so the snapshot matches what the
        main LLM saw. Only the configured modes read anything; "off" is a no-op.
        Writes are additive: an empty later capture never erases a good one.
        """
        mode = context_utils.normalize_context_mode(
            self.config.get(
                "translate_context_mode", context_utils.DEFAULT_CONTEXT_MODE
            )
        )
        if mode == context_utils.DEFAULT_CONTEXT_MODE:
            return

        try:
            messages = context_utils.extract_recent_messages(
                getattr(req, "contexts", None)
            )
            if messages:
                event.set_extra("_tvls_context_messages", messages)
        except Exception:
            logger.debug(
                "[text_voice_lang_split] Failed to snapshot translation context",
                exc_info=True,
            )

        try:
            conversation = getattr(req, "conversation", None)
            if conversation is None:
                # Core applies no persona when there is no conversation.
                return
            resolver = getattr(
                getattr(self.context, "persona_manager", None),
                "resolve_selected_persona",
                None,
            )
            if not callable(resolver):
                if not self._persona_api_warned:
                    self._persona_api_warned = True
                    logger.warning(
                        "[text_voice_lang_split] This AstrBot version has no "
                        "PersonaManager.resolve_selected_persona; persona "
                        "injection is unavailable (context injection still works)"
                    )
                return
            provider_settings = self.context.get_config(event.unified_msg_origin).get(
                "provider_settings", {}
            )
            persona_prompt = await context_utils.resolve_persona_prompt(
                self.context,
                event,
                getattr(conversation, "persona_id", None),
                provider_settings=provider_settings,
                special_default_prompt=CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT,
            )
            if persona_prompt:
                event.set_extra("_tvls_persona_prompt", persona_prompt)
        except Exception:
            logger.debug(
                "[text_voice_lang_split] Failed to resolve the persona prompt",
                exc_info=True,
            )

    async def terminate(self):
        logger.info("[text_voice_lang_split] Plugin terminated")
        self._terminated = True
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
        self._bg_tasks.clear()
        self._temp_manager.cleanup_all()
        self._streaming_texts.clear()
