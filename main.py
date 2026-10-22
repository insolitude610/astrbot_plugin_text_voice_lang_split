import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
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

from . import context_utils, text_utils, voice_utils
from .tools import VoiceTool
from .tools.voice_tool import build_description


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
            # Text only here; the voice is produced once, after the whole turn
            # has been delivered. GENERAL_RESULT also blocks the built-in TTS,
            # so the plugin stays the only source of audio for this message.
            logger.debug(
                "[text_voice_lang_split] Voice tool mode: delivering text only; "
                "the voice is serviced at turn end"
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
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

    async def _maybe_send_turn_voice(self, event: AstrMessageEvent) -> None:
        """Send the turn's voice once, after the turn's text has been delivered.

        Every exit either spawns a voice or logs a WARNING with the reason, so
        a requested voice can never disappear silently.
        """
        if not self.config.get("enable_llm_voice_tool", False):
            return
        if event.get_extra("_tvls_deferred_voice_sent", False):
            return
        if not event.get_extra("_tvls_voice_requested", False):
            return

        event.set_extra("_tvls_deferred_voice_sent", True)

        text = self.delivered_voice_text(event)
        if not text:
            logger.warning(
                "[text_voice_lang_split] Voice requested but no delivered text "
                "was recorded; no voice sent"
            )
            return

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
