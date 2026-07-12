# AGENTS.md

## Architecture

This is an AstrBot plugin that intercepts LLM responses to translate text before TTS, enabling display in one language (e.g. Chinese) and speech in another (e.g. Japanese).

Two separate pipeline hooks handle different output modes:

| Mode | Hook | Flow |
|------|------|------|
| Non-streaming (`LLM_RESULT`) | `@filter.on_decorating_result(priority=999)` | Filter non-speakable content → check speakable length → check `tts_max_chars` (on filtered text) → translate → call TTS → append `Record` to chain → set `ResultContentType.GENERAL_RESULT` + `use_t2i_=False` |
| Streaming (`STREAMING_RESULT`) | `@filter.on_llm_response` → `@filter.after_message_sent` | Capture `LLMResponse.completion_text` → after message sent, filter + translate + TTS + send voice as follow-up |

Critical: `on_decorating_result` does NOT fire for `STREAMING_RESULT` (ResultDecorateStage skips it). The streaming path is the fallback for platforms that buffer streaming output (e.g. QQ个人号 via aiocqhttp).

## Pipeline hook execution order

Hooks fire in this exact sequence per message lifecycle. Understanding this order is critical for double-fire prevention:

```
ProcessStage {
    Agent runner → LLM generates response
    MainAgentHooks.on_agent_done():
        → OnLLMResponseEvent  (on_llm_response fires here)
        → OnAgentDoneEvent
}
ResultDecorateStage {
    if NOT STREAMING_RESULT:
        → OnDecoratingResultEvent  (on_decorating_result fires here)
        → reply prefix / segmented reply
        → BUILT-IN TTS (checks result.is_llm_result())
        → t2i (checks result.use_t2i_)
}
RespondStage {
    [message sent to platform(s)]
    → OnAfterMessageSentEvent  (after_message_sent fires here)
}
```

In non-streaming mode, ALL THREE hooks (`on_llm_response`, `on_decorating_result`, `after_message_sent`) fire. The plugin's double-TTS guard relies on `on_decorating_result` popping `_streaming_texts` so `after_message_sent` finds it empty.

## on_decorating_result exit paths

Seven return paths — this is a frequent source of bugs. ALL success/failure paths that reach the core flow must pop `self._streaming_texts` to prevent `after_message_sent` from double-firing.

| Path | Trigger | Blocks built-in TTS? | Effect |
|------|---------|---------------------|--------|
| No result/chain | `not result or not result.chain` | No (returns before setting) | Early return — `_streaming_texts` NOT popped, streaming path serves as fallback |
| Not LLM result | `not result.is_llm_result()` | N/A (built-in also skips) | Early return — non-LLM content |
| No TTS provider | `get_using_tts_provider()` returns None | No (returns before setting) | Early return — no provider available, streaming path also skips |
| No plain texts | Chain has no `Plain` components | No (returns before setting) | Early return — `_streaming_texts` NOT popped |
| Nothing speakable after filtering | `_filter_text_for_tts()` returns < 2 chars | Yes (`GENERAL_RESULT`) | Silent, text only |
| Exceeds `tts_max_chars` | `len(filtered_text) > max_chars` (checked AFTER filtering) | Yes (`GENERAL_RESULT`) | Silent, no voice |
| Translation failed | `_translate_text` returns `None` | Yes (`GENERAL_RESULT`) | Silent, text only |
| TTS returned empty path | `get_audio()` returns `""` | Yes (`GENERAL_RESULT`) | Silent, no voice |
| TTS generation failed | `tts_provider.get_audio()` raises | Yes (`GENERAL_RESULT`) | Silent, no voice |
| Success | Appends `Record` to chain | Yes (`GENERAL_RESULT`) | Translated voice sent |

Note: the first four paths are "early returns" that do NOT pop `_streaming_texts` or change `result_content_type`. In particular, the "no plain texts" path allows the streaming `after_message_sent` path to serve as a fallback — the raw `completion_text` from `on_llm_response` may differ from `result.chain` Plain text.

## Double-TTS prevention

In non-streaming mode, `on_llm_response`, `on_decorating_result`, AND `after_message_sent` all fire. The guard: `on_decorating_result` pops `self._streaming_texts` so `after_message_sent` finds `None` and returns immediately. NEVER add a return path in `on_decorating_result` that skips the pop without a deliberate reason.

## Agent Live Mode conflict

**Do NOT use this plugin in WebChat's Live Chat interface.** Live Chat sets `event.extra["action_type"] = "live"`, which triggers AstrBot's agent `_simulated_stream_tts` — a built-in per-sentence TTS that makes `get_audio()` calls for each sentence. Running the plugin alongside this produces duplicate TTS requests (agent's N calls + plugin's 1 call), easily hitting API rate limits (429).

The plugin detects `action_type == "live"` in `after_message_sent` and skips its own TTS. The `on_decorating_result` hook already doesn't fire (ResultDecorateStage skips STREAMING_RESULT). This means the plugin is effectively disabled in Live Chat — intentional, since the agent handles TTS natively there.

Normal platform chats (QQ, WeChat, etc.) do NOT set `action_type` and are unaffected.

## Built-in TTS / t2i prevention mechanism

Two independent mechanisms prevent AstrBot's built-in TTS and t2i from firing after the plugin handles them:

1. **`result.result_content_type = ResultContentType.GENERAL_RESULT`**: The built-in TTS in `ResultDecorateStage` checks `result.is_llm_result()`. Setting to `GENERAL_RESULT` makes this return `False`, disabling built-in TTS.

2. **`result.use_t2i_ = False`**: The t2i check in `ResultDecorateStage` evaluates `result.use_t2i_ is None and config["t2i"]) or result.use_t2i_`. With `use_t2i_ = False`, this evaluates to `False`, disabling text-to-image.

These must be set on EVERY return path in `on_decorating_result` that reaches the core flow (from "nothing speakable" onward). They are NOT needed in `after_message_sent` (fires after message is already sent).

## TTS provider availability

`get_using_tts_provider()` in AstrBot's ProviderManager checks `provider_tts_settings.enable`:

```python
if not config["provider_tts_settings"].get("enable"):
    return None
```

Turning off the TTS master switch in AstrBot WebUI causes `get_using_tts_provider()` to return `None`, disabling both the plugin's TTS and the built-in TTS. This is the intended way to globally disable TTS.

## Session lock guarantee

AstrBot acquires a session lock per `unified_msg_origin` before processing a message. This means messages in the same session are processed STRICTLY sequentially. `self._streaming_texts[unified_msg_origin]` is therefore safe from concurrent overwrites — a second message's `on_llm_response` will not fire until the first message's entire pipeline (including `after_message_sent`) completes.

## Text filtering before translation

`_filter_text_for_tts()` runs locally (zero LLM cost) before translation in both hooks. It strips visual noise in 5 steps — **order matters** (step 3 must run before step 4 for correct link handling):

1. Code blocks (`` ```...``` ``, `` `...` ``)
2. Markdown links → keep display text (`[text](url)` → `text`)
3. URLs (`https?://...`)
4. Markdown formatting symbols (`**`, `*`, `_`, `~~`)
5. User-configured regex patterns from `remove_patterns` config

Step 2 uses `[^)]*` (zero or more) to also clean up empty parens that could result from edge cases.

Patterns are compiled at `__init__` via `_compile_filter_patterns()`. Invalid regex is logged as a warning, not raised.

Filtered text is what the LLM sees during translation. The **unfiltered original** text is what the user sees (displayed message chain is never touched).

## `tts_max_chars` — checked on filtered text

The `tts_max_chars` limit is checked on `filtered_text` (AFTER `_filter_text_for_tts()`), not on the original text. If the original contains large code blocks or markdown noise, those are stripped before the length check, so they don't falsely trigger the limit.

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

Recommend setting `translate_provider` to a different provider than the main chat provider to avoid race conditions on rate-limited APIs.

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
