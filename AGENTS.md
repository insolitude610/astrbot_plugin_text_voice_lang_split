# AGENTS.md

## Architecture

This is an AstrBot plugin that intercepts LLM responses to translate text before TTS, enabling display in one language (e.g. Chinese) and speech in another (e.g. Japanese).

Two separate pipeline hooks handle different output modes:

| Mode | Hook | Flow |
|------|------|------|
| Non-streaming (`LLM_RESULT`) | `@filter.on_decorating_result(priority=999)` | Filter non-speakable content → check speakable length → check `tts_max_chars` (on filtered text) → check session TTS → translate → strip thinking artifacts → call TTS → append `Record` to chain → set `ResultContentType.GENERAL_RESULT` + `use_t2i_=False` |
| Streaming (`STREAMING_RESULT`) | `@filter.on_llm_response` via async_stream wrapper | Capture `LLMResponse.completion_text` → wrap `result.async_stream` with injecting generator → stream text → after all chunks yielded, check session TTS → filter + translate + TTS + send voice as follow-up |

Critical: `on_decorating_result` does NOT fire for `STREAMING_RESULT` (ResultDecorateStage skips it). Streaming voice is injected via async generator wrapper — `after_message_sent` is NOT used for streaming because RespondStage returns before firing it for `STREAMING_RESULT`.

Note: platforms like QQ个人号 buffer streaming API text and send it all at once, which is treated as non-streaming by AstrBot — `on_decorating_result` fires normally for those.

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

In streaming mode, `on_llm_response` wraps `async_stream` to inject voice sending; `after_message_sent` does NOT fire (RespondStage returns for `STREAMING_RESULT` before reaching the `OnAfterMessageSentEvent` dispatch). Voice is sent by the wrapper during `send_streaming`.

## on_decorating_result exit paths

Ten return paths — this is a frequent source of bugs. ALL success/failure paths that reach the core flow must pop `self._streaming_texts` to prevent `after_message_sent` from double-firing.

| Path | Trigger | Blocks built-in TTS? | Effect |
|------|---------|---------------------|--------|
| No result/chain | `not result or not result.chain` | No (returns before setting) | Early return — `_streaming_texts` NOT popped |
| Not LLM result | `not result.is_llm_result()` | N/A (built-in also skips) | Early return — non-LLM content |
| No TTS provider | `get_using_tts_provider()` returns None | No (returns before setting) | Early return — no provider available |
| Session TTS disabled | `should_process_tts_request()` returns False | No (returns before setting) | Early return — session opted out of TTS |
| No plain texts | Chain has no `Plain` components | No (returns before setting) | Early return — `_streaming_texts` NOT popped |
| Nothing speakable after filtering | `_filter_text_for_tts()` returns < 2 chars | Yes (`GENERAL_RESULT`) | Silent, text only |
| Exceeds `tts_max_chars` | `len(filtered_text) > max_chars` (checked AFTER filtering) | Yes (`GENERAL_RESULT`) | Silent, no voice |
| Translation failed | `_translate_text` returns `None` (timeout, error, or thinking-only output stripped empty) | Yes (`GENERAL_RESULT`) | Silent, text only |
| TTS returned empty path | `get_audio()` returns `""` | Yes (`GENERAL_RESULT`) | Silent, no voice |
| TTS generation failed | `tts_provider.get_audio()` raises | Yes (`GENERAL_RESULT`) | Silent, no voice |
| Success | Appends `Record` to chain | Yes (`GENERAL_RESULT`) | Translated voice sent |

Note: the first five paths are "early returns" that do NOT pop `_streaming_texts` or change `result_content_type`. The `after_message_sent` hook is now simplified to only pop stale entries — non-streaming voice is handled in `on_decorating_result`, streaming voice is handled by the async_stream wrapper in `on_llm_response`.

## Double-TTS prevention

In non-streaming mode, `on_llm_response`, `on_decorating_result`, AND `after_message_sent` all fire. The guard: `on_decorating_result` pops `self._streaming_texts` so `after_message_sent` finds `None` and returns immediately. NEVER add a return path in `on_decorating_result` that skips the pop without a deliberate reason.

## Agent Live Mode conflict

**Do NOT use this plugin in WebChat's Live Chat interface.** Live Chat sets `event.extra["action_type"] = "live"`, which triggers AstrBot's agent `_simulated_stream_tts` — a built-in per-sentence TTS that makes `get_audio()` calls for each sentence. Running the plugin alongside this produces duplicate TTS requests (agent's N calls + plugin's 1 call), easily hitting API rate limits (429).

The plugin detects `action_type == "live"` in `_send_streaming_follow_up` (called by the async_stream wrapper and also reachable via non-streaming paths) and skips its own TTS. The `on_decorating_result` hook already doesn't fire (ResultDecorateStage skips STREAMING_RESULT). This means the plugin is effectively disabled in Live Chat — intentional, since the agent handles TTS natively there.

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

The plugin also checks `SessionServiceManager.should_process_tts_request(event)` (matching AstrBot's built-in TTS check) to respect session-level TTS configuration. This is checked in both `on_decorating_result` and `_send_streaming_follow_up` before any translation/TTS work.

## Session lock guarantee

AstrBot acquires a session lock per `unified_msg_origin` before processing a message. This means messages in the same session are processed STRICTLY sequentially. `self._streaming_texts[unified_msg_origin]` is therefore safe from concurrent overwrites — a second message's `on_llm_response` will not fire until the first message's entire pipeline (including `after_message_sent`) completes.

## Text filtering before translation

`_filter_text_for_tts()` runs locally (zero LLM cost) before translation in both hooks. It strips visual noise in 5 steps — **order matters** (step 2 must run before step 3 for correct link handling):

1. Code blocks (`` ```...``` ``, `` `...` ``)
2. Markdown links → keep display text (`[text](url)` → `text`)
3. URLs — ASCII-safe pattern (`https?://[a-zA-Z0-9./?#&=\-+%:!*'();,@[\]~_$]+`) to avoid eating CJK characters after URL
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
- Each retry creates a fresh coroutine (`self.context.llm_generate(...)` called inside the loop), so a cancelled attempt does not leak state into the next one

## Translation timeout and retry

`_translate_text` wraps `llm_generate` with `asyncio.wait_for` and retries ONCE on timeout:

```
attempt 1: await asyncio.wait_for(llm_generate(...), timeout)
  ↓ TimeoutError
await asyncio.sleep(0.5)  ← give httpx connection pool time to purge stale connections
  ↓
attempt 2: await asyncio.wait_for(llm_generate(...), timeout)
  ↓ TimeoutError again → give up, return None
  ↓ Success → return completion_text
```

The 0.5s delay addresses an httpx connection pool issue: when `asyncio.wait_for` forcibly cancels a coroutine mid-HTTP-request, the underlying TCP connection may NOT be properly closed by httpx. The stale connection stays in the pool and gets re-used by the retry, causing another hang. `asyncio.sleep(0.5)` lets the event loop process the `CancelledError` cleanup and purge the bad connection before retrying.

### Root cause fix (not in plugin)

The real fix is **provider-level**: set `timeout: 15` on the translation provider's config in AstrBot WebUI (default is 120). This makes httpx handle timeouts internally via `httpx.ReadTimeout` — which it CAN clean up properly — instead of `asyncio.wait_for` forcibly cancelling from outside.

With the provider timeout set low:
- httpx raises `ReadTimeout` → closes the TCP connection cleanly → no dirty connection in pool → retry always starts fresh
- Total worst-case latency: ~15s (provider timeout) + 0.5s + ~15s ≈ 31s, vs ~60s+ without the fix

The plugin's retry is the **safety net**; the provider timeout is the **real fix**.

## Thinking/reasoning defense (v1.3.0+)

Reasoning models (DeepSeek-R1, Gemini, etc.) may output internal monologue as part of `completion_text`, which gets spoken by TTS if not stripped. Two complementary layers prevent this:

1. **`system_prompt`**: `_translate_text` passes a strict system prompt via `llm_generate(system_prompt=...)` that explicitly forbids reasoning output. This prevents most thinking at the source.

2. **`_strip_thinking()`**: A `@staticmethod` that post-processes `completion_text` with regex — strips `<think>...</think>` blocks, strips content before `` markers, cleans orphan `</think>` tags, and collapses excess whitespace. Runs on the **translation output** (NOT the original text).

If `_strip_thinking()` returns an empty string (everything was reasoning), the result is treated as translation failure (`return self._strip_thinking(raw) or None`), triggering the silent fallback path.

The Gemini provider (`gemini_source.py`) relies exclusively on `part.thought == True` to separate thinking — it has NO `<think>` regex fallback unlike the OpenAI source. Community API proxies may strip the `thought` attribute. The plugin's `_strip_thinking` is the last line of defense regardless of provider quirks.

## Provider resolution

`_translate_text` resolves the provider in 3-tier priority:

```
1. translate_provider config     (user explicitly set in WebUI)
2. event.selected_provider       (chat /provider command override)
3. get_current_chat_provider_id() (default fallback)
```

Recommend setting `translate_provider` to a different provider than the main chat provider to avoid race conditions on rate-limited APIs. Also set that provider's `timeout` to **15** (not the default 120) per the timeout section above.

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

# Push (if behind proxy, set first)
# git config http.proxy http://127.0.0.1:10808
# git config https.proxy http://127.0.0.1:10808
git push origin main
```

## Constraints

- No external dependencies — all APIs are AstrBot built-ins
- AstrBot >= 4.5.7 required (`llm_generate` API introduced here)
- Python 3.10+ (AstrBot baseline)
