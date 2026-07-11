# AGENTS.md

## Architecture

This is an AstrBot plugin that intercepts LLM responses to translate text before TTS, enabling display in one language (e.g. Chinese) and speech in another (e.g. Japanese).

Two separate pipeline hooks handle different output modes:

| Mode | Hook | Flow |
|------|------|------|
| Non-streaming (`LLM_RESULT`) | `@filter.on_decorating_result(priority=999)` | Translate text → call TTS provider → append `Record` to chain → set `ResultContentType.GENERAL_RESULT` + `use_t2i_=False` |
| Streaming (`STREAMING_RESULT`) | `@filter.on_llm_response` → `@filter.after_message_sent` | Capture `LLMResponse.completion_text` → after message sent, translate + TTS + send voice as follow-up |

Critical: `on_decorating_result` does NOT fire for `STREAMING_RESULT` (ResultDecorateStage skips it). The streaming path is the fallback for platforms that buffer streaming output (e.g. QQ个人号 via aiocqhttp).

## on_decorating_result exit paths

Four return paths with different behaviors — this is a frequent source of bugs:

| Path | Trigger | Blocks built-in TTS? | Effect |
|------|---------|---------------------|--------|
| Translation failed | `_translate_text` returns `None` | No | Original-language TTS fallback |
| Exceeds `tts_max_chars` | `len(translated) > max_chars` | Yes (`use_t2i_=False`) | Silent, no voice |
| TTS generation failed | `tts_provider.get_audio()` raises | Yes (`use_t2i_=False`) | Silent, no voice |
| Success | Appends `Record` to chain | Yes (`use_t2i_=False`) | Translated voice sent |

All paths must pop `self._streaming_texts` to prevent `after_message_sent` from double-firing. The translation-failure path intentionally does NOT set `use_t2i_=False` so AstrBot's built-in TTS reads the original text as a fallback.

## Double-TTS prevention

In non-streaming mode, both `on_llm_response` AND `after_message_sent` fire. `on_decorating_result` pops `self._streaming_texts` (after appending the `Record` to the chain) to prevent `after_message_sent` from firing a second TTS. If modifying these handlers, preserve this guard.

## llm_generate isolation

`self.context.llm_generate()` is a bare-metal LLM call — it does NOT trigger any filter hooks (`on_llm_response`, `on_decorating_result`, `after_message_sent`, `on_llm_request`). This means:

- Translation calls do NOT pollute `self._streaming_texts` or trigger other plugins
- Other plugins' hooks do NOT fire on translation requests
- `llm_generate` bypasses session locks, rate limiting, and the agent runner entirely

## Provider resolution

`_translate_text` resolves the provider in 3-tier priority:

```
1. translate_provider config     (user explicitly set in WebUI)
2. event.selected_provider       (chat /provider command override)
3. get_current_chat_provider_id() (default fallback)
```

## Import paths (non-obvious)

```python
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
```

`ResultContentType` is NOT exported via `astrbot.api.all` — must import from `astrbot.core.message.message_event_result`.

## Config schema gotcha

`_conf_schema.json` uses AstrBot's config type system. Supported types: `int`, `float`, `bool`, `string`, `text`, `list`, `file`, `object`, `template_list`. Using `"integer"` instead of `"int"` causes a `TypeError` on plugin load.

## Commands

```bash
# Format and lint (run from AstrBot project root)
ruff format data/plugins/astrbot_plugin_text_voice_lang_split/
ruff check data/plugins/astrbot_plugin_text_voice_lang_split/

# Push (proxy required for GitHub)
git config http.proxy http://127.0.0.1:10808
git config https.proxy http://127.0.0.1:10808
git push origin main
```

## Constraints

- No external dependencies — all APIs are AstrBot built-ins
- AstrBot >= 4.5.7 required (`llm_generate` API introduced here)
- Python 3.10+ (AstrBot baseline)
