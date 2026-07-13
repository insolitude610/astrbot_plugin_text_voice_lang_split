# AGENTS.md

## Architecture

AstrBot plugin that translates LLM text before TTS, enabling display in one language and speech in another. Three pipeline hooks handle different output modes:

| Mode | Hook | Mechanism |
|------|------|-----------|
| Non-streaming | `@filter.on_decorating_result(priority=999)` | Filter → translate → TTS → append `Record` → set `GENERAL_RESULT` |
| Streaming | `@filter.on_llm_request` → `@filter.on_llm_response` | `on_llm_request` patches `event.send_streaming` with `finally` voice follow-up; `on_llm_response` stores text |
| Guard | `@filter.after_message_sent(priority=999)` | Only pops stale `_streaming_texts` entries (double-TTS prevention) |

Key: `on_decorating_result` does NOT fire for `STREAMING_RESULT`. `after_message_sent` does NOT fire for `STREAMING_RESULT` (RespondStage returns before dispatch). Streaming voice is sent by the patched `send_streaming` wrapper.

Note: QQ个人号 buffers streaming API text and sends all at once — AstrBot treats this as non-streaming, so `on_decorating_result` fires.

## Pipeline hook order

```
OnLLMRequestEvent → on_llm_request (patches send_streaming here!)
    ↓
ProcessStage { agent runs → on_llm_response fires }
    ↓
ResultDecorateStage { on_decorating_result fires (non-streaming only) }
    ↓
RespondStage { send_streaming called → patched wrapper sends voice after stream }
    ↓
OnAfterMessageSentEvent → after_message_sent (non-streaming only)
```

## Streaming voice mechanism (v1.5.0+)

`on_llm_request` runs BEFORE agent. It monkey-patches `event.send_streaming`:

```python
event.send_streaming = lambda stream, *a, **kw: (
    await original(stream, *a, **kw);  # finish
    await self._send_streaming_follow_up(event, session_key)  # voice
)
```

This is superior to the old async_stream wrapper approach because it patches before the stream is created, not during iteration. Guarded by `_tvls_stream_patched` flag to prevent double-patching.

## `_strip_thinking` regex — CRITICAL

```python
text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
text = re.sub(r"^.*?\s*response", "", text, flags=re.DOTALL)
```

The `\s*response` pattern strips DeepSeek-R1's ` response` separator. **Do NOT change to `\s*\presponse`** — `\p` is invalid in Python 3.12 regex and will crash `_strip_thinking` silently (caught by `except Exception` in `_translate_text`, causing all translations to fail).

## `is_llm_result()` guard — DO NOT REMOVE

Line 147: `if not result.is_llm_result(): return`. This deliberately excludes:
- **Proactive chat messages** (they're `GENERAL_RESULT` and have their own TTS)
- **Command responses** (`/provider list`, etc — should never be voiced)

v1.5.2 attempted to replace this with chain inspection (`has_plain`/`has_record`) and was immediately reverted — it would have caused double TTS with proactive chat and unwanted TTS on command output.

## Emotion tags — English only

FishAudio S2 uses natural-language `[bracket]` cues. English words are recognized; Japanese words (`[嬉しい]`) are spoken as text. A curated list of 28 preferred tags is embedded in the translation prompt, plus intensity modifiers (`[slightly]`, `[very]`, `[extremely]`).

## `GENERAL_RESULT` side effects

Setting `result.result_content_type = ResultContentType.GENERAL_RESULT`:
- ✅ Blocks built-in TTS (`is_llm_result()` returns False)
- ❌ Breaks segmented replies (`is_model_result()` returns False)
- ❌ RespondStage extracts and sends `Record` components first → voice arrives before text

These are framework-level tradeoffs. Three approaches have been evaluated and rejected:

1. **`SessionServiceManager.set_tts_status_for_session()` temporary toggle** (v1.5.2): if `after_message_sent` doesn't fire (pipeline error), TTS stays permanently disabled in persistent storage.

2. **Session-level TTS suppression with lease + reference count + watchdog + token exactly-once** (v2.0 exploration, 2026-07): requires a state machine incorporating persistent storage, leases, timeouts, ref counting, baseline snapshots, atomic state transitions, and crash recovery — effectively a miniature distributed-systems library implemented inside a single plugin. Complexity is indefensible for what amounts to a per-message boolean.

3. **Framework-level `result.use_tts_ = False`**: the clean solution — add a per-message TTS skip flag to AstrBot. Requires upstream PR; plugin can adopt when framework ships it.

**Decision:** Segmented replies are documented as mutually exclusive with this plugin. Users choose one or the other. The `GENERAL_RESULT` mechanism is stable, well-tested, and avoids the entire class of concurrency/persistence/safety-net bugs that session-state manipulation introduces.

## `on_llm_response` — result_chain fallback

Coze, Dify, DashScope runners put text in `resp.result_chain` instead of `resp.completion_text`:

```python
text = resp.completion_text
if not text and resp.result_chain:
    text = resp.result_chain.get_plain_text()
```

## Import paths (non-obvious)

```python
from astrbot.core.message.message_event_result import ResultContentType  # NOT in astrbot.api.all
from astrbot.core.star.session_llm_manager import SessionServiceManager
```

## Config schema gotcha

`_conf_schema.json` types: `int`, `float`, `bool`, `string`, `text`, `list`, `file`, `object`, `template_list`. `"integer"` → `TypeError`.

## Commands

```bash
ruff format data/plugins/astrbot_plugin_text_voice_lang_split/
ruff check data/plugins/astrbot_plugin_text_voice_lang_split/
git push origin main
```

Run from AstrBot project root.

## Constraints

- AstrBot >= 4.22.0 (`track_temporary_local_file` introduced here)
- Python 3.10+ (AstrBot baseline)
- No external dependencies
