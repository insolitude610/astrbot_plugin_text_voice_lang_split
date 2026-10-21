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


class TextVoiceLangSplit(Star):
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
        self.context.add_llm_tools(self._voice_tool)

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

        if event.get_extra("_tvls_decorated", False):
            await self._maybe_send_deferred_voice(event)
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
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

        filtered_text = text_utils.filter_text_for_tts(full_text, self._filter_patterns)
        if len(filtered_text.strip()) < 2:
            logger.info(
                "[text_voice_lang_split] Nothing speakable after filtering, skip TTS"
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        max_chars = self.config.get("tts_max_chars", 0)
        if max_chars > 0 and len(filtered_text) > max_chars:
            logger.info(
                f"[text_voice_lang_split] Filtered text ({len(filtered_text)} chars) "
                f"exceeds max ({max_chars}), skip TTS"
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        if self.config.get("enable_llm_voice_tool", False):
            if not event.get_extra("_tvls_voice_requested", False):
                logger.debug(
                    "[text_voice_lang_split] Voice tool enabled but LLM did not "
                    "request voice, storing pending voice"
                )
                event.set_extra("_tvls_pending_text", filtered_text)
                result.result_content_type = ResultContentType.GENERAL_RESULT
                result.use_t2i_ = False
                self._streaming_texts.pop(self._get_session_key(event), None)
                return

        logger.info(f"[text_voice_lang_split] Translating: '{full_text[:50]}...'")

        voice = await voice_utils.produce_voice_paths(
            self.context,
            self.config,
            event,
            filtered_text,
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
        # Capture before the streaming-patch guard: the guard returns early on
        # later calls, and the snapshot must exist for every voice path.
        await self._capture_translation_context(event, req)
        if getattr(event, "_tvls_stream_patched", False):
            return
        original = event.send_streaming

        async def _patched(stream, *args, **kwargs):
            try:
                await original(stream, *args, **kwargs)
            finally:
                await voice_utils.send_streaming_follow_up(
                    self.context,
                    self.config,
                    event,
                    event.unified_msg_origin,
                    self._streaming_texts,
                    self._filter_patterns,
                    self._tts_semaphore,
                )

        event.send_streaming = _patched
        event._tvls_stream_patched = True

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        text = resp.completion_text
        if not text and resp.result_chain:
            text = resp.result_chain.get_plain_text()
        if not text:
            return

        session_key = self._get_session_key(event)
        self._streaming_texts[session_key] = text

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

    async def _maybe_send_deferred_voice(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable_llm_voice_tool", False):
            return
        if not await voice_utils.is_tvls_enabled(event):
            return
        if not event.get_extra("_tvls_voice_requested", False):
            return
        pending = event.get_extra("_tvls_pending_text", None)
        if not pending:
            return
        if event.get_extra("_tvls_deferred_voice_sent", False):
            return
        event.set_extra("_tvls_deferred_voice_sent", True)
        event.clear_extra("_tvls_pending_text")
        logger.info(
            "[text_voice_lang_split] Deferred voice triggered "
            f"(pending text: '{pending[:50]}...')"
        )
        self._spawn_voice_task(
            voice_utils.send_deferred_voice(
                self.context,
                self.config,
                event,
                pending,
                self._tts_semaphore,
                self._temp_manager,
            )
        )

    def _spawn_voice_task(self, coro) -> asyncio.Task | None:
        """Register a background voice task; refuse after termination begins."""
        if self._terminated:
            logger.debug(
                "[text_voice_lang_split] Plugin terminating, refuse to spawn voice task"
            )
            return None
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @filter.after_message_sent(priority=999)
    async def after_message_sent(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        self._streaming_texts.pop(session_key, None)
        await self._maybe_send_deferred_voice(event)

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

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

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
