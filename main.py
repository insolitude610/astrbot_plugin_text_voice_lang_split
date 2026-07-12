import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class TextVoiceLangSplit(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._streaming_texts: dict[str, str] = {}

    async def initialize(self):
        logger.info("[text_voice_lang_split] Plugin initialized")

    async def _translate_text(self, text: str, event: AstrMessageEvent) -> str | None:
        voice_lang = self.config.get("voice_language", "Japanese")
        custom_instructions = self.config.get("translate_instructions", "")

        prompt = (
            f"Translate the following text into {voice_lang}. "
            f"At the beginning of each sentence, insert a suitable emotion tag "
            f"in {voice_lang} wrapped in square brackets "
            f"(e.g. [嬉しい], [悲しい] in Japanese, [happy], [sad] in English). "
            f"Choose tags based on the emotional tone of the original text. "
            f"If a sentence has no clear emotion, omit the tag. "
            f"Only output the translated text with tags, nothing else."
        )
        if custom_instructions:
            prompt = f"{prompt}\nAdditional instructions: {custom_instructions}"

        try:
            provider_id = self.config.get("translate_provider", "").strip()
            if not provider_id:
                provider_id = event.get_extra("selected_provider")
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            logger.debug(
                f"[text_voice_lang_split] Translating with provider: {provider_id}"
            )
            coro = self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{prompt}\n\nText: {text}",
            )
            timeout = self.config.get("translate_timeout", 30.0)
            if timeout > 0:
                llm_resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                llm_resp = await coro
            return llm_resp.completion_text.strip()
        except asyncio.TimeoutError:
            logger.warning(
                f"[text_voice_lang_split] Translation timed out after {timeout}s, falling back"
            )
            return None
        except Exception:
            logger.warning(
                "[text_voice_lang_split] Translation failed, falling back",
                exc_info=True,
            )
            return None

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    @filter.on_decorating_result(priority=999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return

        if not result.is_llm_result():
            return

        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            logger.debug("[text_voice_lang_split] No TTS provider configured, skip")
            return

        plain_texts: list[tuple[int, Plain]] = []
        for i, comp in enumerate(result.chain):
            if isinstance(comp, Plain) and comp.text.strip():
                plain_texts.append((i, comp))

        if not plain_texts:
            return

        full_text = "".join(comp.text for _, comp in plain_texts)
        if len(full_text.strip()) < 2:
            return

        max_chars = self.config.get("tts_max_chars", 0)
        if max_chars > 0 and len(full_text) > max_chars:
            logger.info(
                f"[text_voice_lang_split] Original text ({len(full_text)} chars) "
                f"exceeds max ({max_chars}), skip TTS"
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        logger.info(f"[text_voice_lang_split] Translating: '{full_text[:50]}...'")

        translated = await self._translate_text(full_text, event)
        if not translated:
            self._streaming_texts.pop(self._get_session_key(event), None)
            try:
                audio_path = await tts_provider.get_audio(full_text)
                event.track_temporary_local_file(audio_path)
                result.chain.append(
                    Record(file=audio_path, url=audio_path, text=full_text)
                )
                result.result_content_type = ResultContentType.GENERAL_RESULT
                result.use_t2i_ = False
                logger.info(
                    "[text_voice_lang_split] Translation failed, fallback TTS with original text"
                )
            except Exception:
                logger.error(
                    "[text_voice_lang_split] Translation failed and fallback TTS also failed",
                    exc_info=True,
                )
            return

        try:
            audio_path = await tts_provider.get_audio(translated)
            event.track_temporary_local_file(audio_path)
        except Exception:
            logger.error(
                "[text_voice_lang_split] TTS generation failed, keeping original",
                exc_info=True,
            )
            result.result_content_type = ResultContentType.GENERAL_RESULT
            result.use_t2i_ = False
            self._streaming_texts.pop(self._get_session_key(event), None)
            return

        result.chain.append(Record(file=audio_path, url=audio_path, text=translated))
        result.result_content_type = ResultContentType.GENERAL_RESULT
        result.use_t2i_ = False
        self._streaming_texts.pop(self._get_session_key(event), None)

        logger.info("[text_voice_lang_split] Voice appended to result chain")

    @filter.after_message_sent(priority=999)
    async def after_message_sent(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        accumulated = self._streaming_texts.pop(session_key, None)
        if accumulated is None:
            return

        if not accumulated.strip() or len(accumulated.strip()) < 2:
            return

        tts_provider = self.context.get_using_tts_provider(event.unified_msg_origin)
        if not tts_provider:
            return

        max_chars = self.config.get("tts_max_chars", 0)
        if max_chars > 0 and len(accumulated) > max_chars:
            logger.info(
                f"[text_voice_lang_split] Original text ({len(accumulated)} chars) "
                f"exceeds max ({max_chars}), skip TTS"
            )
            return

        logger.info(
            f"[text_voice_lang_split] Streaming: translating '{accumulated[:50]}...'"
        )

        translated = await self._translate_text(accumulated, event)
        if not translated:
            logger.info(
                "[text_voice_lang_split] Streaming translation failed, fallback TTS with original text"
            )
            try:
                audio_path = await tts_provider.get_audio(accumulated)
                event.track_temporary_local_file(audio_path)
                delay = self.config.get("streaming_follow_up_delay", 1.5)
                await asyncio.sleep(delay)
                chain = MessageChain()
                chain.chain = [
                    Record(file=audio_path, url=audio_path, text=accumulated)
                ]
                await self.context.send_message(event.unified_msg_origin, chain)
            except Exception:
                logger.error(
                    "[text_voice_lang_split] Streaming fallback TTS also failed",
                    exc_info=True,
                )
            return

        try:
            audio_path = await tts_provider.get_audio(translated)
            event.track_temporary_local_file(audio_path)
        except Exception:
            logger.error(
                "[text_voice_lang_split] Streaming TTS generation failed",
                exc_info=True,
            )
            return

        delay = self.config.get("streaming_follow_up_delay", 1.5)
        await asyncio.sleep(delay)

        chain = MessageChain()
        chain.chain = [Record(file=audio_path, url=audio_path, text=translated)]
        await self.context.send_message(event.unified_msg_origin, chain)

        logger.info("[text_voice_lang_split] Streaming voice sent as follow-up")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        text = resp.completion_text
        if not text:
            return

        session_key = self._get_session_key(event)
        self._streaming_texts[session_key] = text

    async def terminate(self):
        logger.info("[text_voice_lang_split] Plugin terminated")
        self._streaming_texts.clear()
