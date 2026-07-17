# AGENTS.md

## Architecture

AstrBot plugin that translates LLM text before TTS, enabling display in one language and speech in another. Three pipeline hooks + deferred voice mechanism handle different output modes:

| Mode | Hook | Mechanism |
|------|------|-----------|
| Non-streaming | `@filter.on_decorating_result(priority=999)` | Filter → translate → TTS → append `Record` → set `GENERAL_RESULT` |
| Streaming | `@filter.on_llm_request` → `@filter.on_llm_response` | `on_llm_request` patches `event.send_streaming` with `finally` voice follow-up; `on_llm_response` stores text |
| Guard | `@filter.after_message_sent(priority=999)` | Cleans `_streaming_texts` + triggers `_maybe_send_deferred_voice` as last-resort fallback |
| Deferred voice (v1.7.0) | Subsequent `on_decorating_result` calls + `after_message_sent` | When LLM calls `tvls_send_voice` after text is already sent by first `on_decorating_result`, deferred voice translates+TTS+ sends as independent follow-up message |

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

## v1.7.0: LLM voice tool + deferred voice

### `tvls_send_voice` FunctionTool

When `enable_llm_voice_tool` is `true`, `initialize()` registers a `VoiceTool` (`tools/voice_tool.py`) via `self.context.add_llm_tools()`. It extends `FunctionTool[AstrAgentContext]` and sets `event.set_extra("_tvls_voice_requested", True)` when LLM calls it. The `active` field is toggled by config. Off → tool hidden from LLM; plugin always generates voice.

### Tool-loop agent runner quirk — CRITICAL

AstrBot's `tool_loop_agent_runner` fires `on_decorating_result` at **each tool-call iteration**, not just once. Typical flow:

```
1. LLM generates text "hello world"
2. on_decorating_result #1 → text sent (no tool marker yet)
3. LLM calls tvls_send_voice → marker set
4. on_decorating_result #2 → same text AGAIN (bug: double output)
```

This caused two bugs fixed in v1.7.0:
- **Double text**: Same message sent twice (first without voice, second with voice)
- **Deferred voice lost**: If first iteration has empty chain and second has text, voice was never sent

### `_tvls_decorated` dedup guard — DO NOT MOVE

Lines 243-247: Guard checks `event.get_extra("_tvls_decorated")`. If set, blocks duplicate output and calls `_maybe_send_deferred_voice(event)`.

```
# At top after is_llm_result() check:
if event.get_extra("_tvls_decorated", False):
    self._maybe_send_deferred_voice(event)
    result.result_content_type = ResultContentType.GENERAL_RESULT
    result.use_t2i_ = False
    return
```

**CRITICAL: `event.set_extra("_tvls_decorated", True)` must be placed AFTER `if not plain_texts: return` (line 273), not before.** If placed before text extraction, empty-chain calls (tool loop iteration with no text) falsely mark the event as processed, permanently blocking subsequent calls with actual text from generating voice.

### Deferred voice event extras

Four extras on `event` control the deferred voice flow:

| Extra key | Set by | Cleared by | Purpose |
|-----------|--------|------------|---------|
| `_tvls_decorated` | First `on_decorating_result` with text | Not cleared | Blocks duplicate calls |
| `_tvls_voice_requested` | `VoiceTool.call()` | Not cleared | LLM called the tool |
| `_tvls_pending_text` | First `on_decorating_result` when gate fires (tool ON, no marker) | `_maybe_send_deferred_voice` on trigger | Text to speak if voice deferred |
| `_tvls_deferred_voice_sent` | `_maybe_send_deferred_voice` on trigger | Not cleared | Prevents double voice send |

`_maybe_send_deferred_voice` is called from 3 sites (subsequent `on_decorating_result`, `after_message_sent`). The `_tvls_deferred_voice_sent` guard prevents double-triggering across all three.

### Deferred voice send

`_send_deferred_voice` is spawned via `asyncio.create_task()`. It independently: gets TTS provider → translates → generates audio → sends as follow-up `MessageChain` with `Record`. Error handling wraps each async step.

## Streaming voice mechanism (v1.5.0+)

`on_llm_request` runs BEFORE agent. It monkey-patches `event.send_streaming`:

```python
event.send_streaming = lambda stream, *a, **kw: (
    await original(stream, *a, **kw);  # finish
    await self._send_streaming_follow_up(event, session_key)  # voice
)
```

Guarded by `_tvls_stream_patched` flag to prevent double-patching. Streaming `_send_streaming_follow_up` also checks `_tvls_voice_requested` when `enable_llm_voice_tool` is ON.

## `_strip_thinking` regex — CRITICAL

```python
text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
text = re.sub(r"^.*?\s*response", "", text, flags=re.DOTALL)
```

The `\s*response` pattern strips DeepSeek-R1's ` response` separator. **Do NOT change to `\s*\presponse`** — `\p` is invalid in Python 3.12 regex and will crash `_strip_thinking` silently (caught by `except Exception` in `_translate_text`, causing all translations to fail).

## `is_llm_result()` guard — DO NOT REMOVE

Line 240: `if not result.is_llm_result(): return`. This deliberately excludes:
- **Proactive chat messages** (they're `GENERAL_RESULT` and have their own TTS)
- **Command responses** (`/provider list`, etc — should never be voiced)

v1.5.2 attempted to replace this with chain inspection and was immediately reverted — it would have caused double TTS with proactive chat and unwanted TTS on command output.

## Emotion tags — English only

FishAudio S2 uses natural-language `[bracket]` cues. English words are recognized; Japanese words (`[嬉しい]`) are spoken as text. A curated list of 28 preferred tags is embedded in the translation prompt, plus intensity modifiers (`[slightly]`, `[very]`, `[extremely]`).

## `GENERAL_RESULT` side effects

Setting `result.result_content_type = ResultContentType.GENERAL_RESULT`:
- ✅ Blocks built-in TTS (`is_llm_result()` returns False)
- ❌ Breaks segmented replies (`is_model_result()` returns False)
- ❌ RespondStage extracts and sends `Record` components first → voice arrives before text

**Decision:** Segmented replies are documented as mutually exclusive with this plugin. Users choose one or the other.

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
from astrbot.core.agent.run_context import ContextWrapper  # for FunctionTool.call()
from astrbot.core.astr_agent_context import AstrAgentContext  # generic param for FunctionTool
from astrbot.api import FunctionTool
```

## Tool registration pattern (v1.7.0)

```python
from dataclasses import dataclass, field

@dataclass
class VoiceTool(FunctionTool[AstrAgentContext]):
    plugin: Any | None = None
    name: str = "tvls_send_voice"
    description: str = "..."
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {}, "required": [],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = context.context.event  # AstrMessageEvent via agent context
        event.set_extra("_tvls_voice_requested", True)
        return "Voice message will be sent."
```

Register in `initialize()` via `self.context.add_llm_tools(tool_instance)`. Registration replaces existing tool with same name. Toggle `tool.active` to show/hide from LLM.

## Config schema gotcha

`_conf_schema.json` types: `int`, `float`, `bool`, `string`, `text`, `list`, `file`, `object`, `template_list`. `"integer"` → `TypeError`.

## Tests

Three test suites, all in `tests/` (gitignored):

```bash
# Run with AstrBot venv (REAL import layer, full integration):
& "D:\AstrBotLauncher-0.1.5.5\AstrBot\venv\Scripts\python.exe" tests/test_voice_tool.py

# Standalone (no AstrBot deps needed, reference layer only):
python tests/test_strip_thinking.py
python tests/test_filter_text_for_tts.py
python tests/test_voice_tool.py
```

`test_voice_tool.py` has dual layers: reference layer (standalone) + integration layer (requires AstrBot venv, uses real imports).

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
