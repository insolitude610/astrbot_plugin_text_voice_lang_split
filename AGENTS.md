# AGENTS.md

## Architecture

AstrBot plugin that translates LLM text before TTS, enabling display in one language and speech in another. Three pipeline hooks + one voice owner per mode:

| Mode | Hook | Mechanism |
|------|------|-----------|
| Non-streaming (tool OFF) | `@filter.on_decorating_result(priority=999)` | Filter → translate → TTS → append `Record` → set `GENERAL_RESULT` |
| Non-streaming (tool ON) | `on_decorating_result` + `on_llm_response(priority=-1000)` + `after_message_sent(priority=999)` | Decoration delivers **text only** (and always sets `GENERAL_RESULT`, which blocks the built-in TTS). `on_llm_response` marks `_tvls_final_seen`; the `after_message_sent` that follows services the voice from the **delivered** text. An exact all-plain repeat of the delivered text after a tool call is suppressed and the voice is serviced immediately |
| Streaming | `@filter.on_llm_request` → `@filter.on_llm_response` | `on_llm_request` patches `event.send_streaming`; the wrapper runs the voice follow-up after the stream (`stream_ok` guards a failed stream) |

Key: `on_decorating_result` does NOT fire for `STREAMING_RESULT`. `after_message_sent` does NOT fire for `STREAMING_RESULT` either (RespondStage returns before dispatch), so streaming keeps its own owner.

Note: QQ个人号 buffers streaming API text and sends all at once — AstrBot treats this as non-streaming, so `on_decorating_result` fires.

## Pipeline hook order

```
OnLLMRequestEvent → on_llm_request (reset turn state, patch send_streaming)
    ↓
ProcessStage { agent runs } ── per step: decorate → respond(send) → after_message_sent
    ↓                         (so a step's text is delivered BEFORE its tool call)
Tool execution ─ loop continues until the final response
    ↓
on_agent_done → on_llm_response (fires ONCE: final/abort response, before its delivery)
    ↓
ResultDecorateStage → RespondStage → after_message_sent (the turn's last message)
```

## Voice tool mode (`enable_llm_voice_tool`) — the current design

### One voice owner

`on_decorating_result` never voices in tool mode and always sets
`result.result_content_type = GENERAL_RESULT` + `use_t2i_ = False`. That single
flag does two jobs: it stops AstrBot's built-in TTS from reading the same reply
(`is_llm_result()` becomes False), and it marks the message as already
decided. The plugin's own voice is produced **later**, once:

```
on_llm_response (priority=-1000)  → _tvls_final_seen = True
                                 → if the final text is empty: service now
after_message_sent (final msg)   → record shipped text, then service
```

### Why the tool cannot promise

`VoiceTool.call()` runs *before* the reply exists (AstrBot yields the assistant
text of a step before executing its tool calls), so it cannot know the final
length. It therefore:

- refuses only what is already certain: unavailable environment
  (`voice_gate_env`), or already-delivered text already over `tts_max_chars`
  (monotonic);
- otherwise records `_tvls_voice_requested` and answers "Voice request
  recorded." — never "will be sent";
- is idempotent: a second call is refused without changing state.

The description (`build_description(max_chars)` in `tools/voice_tool.py`) states
the length as **guidance**, tells the model to write the whole reply first and
call once, and to write nothing afterwards. `main.initialize()` rebuilds it from
the live config, so the model sees the current cap.

### Delivered-text accounting

`after_message_sent` reads `event.get_result()` — still the chain RespondStage
shipped (`clear_result()` runs right after the hook), so sibling handlers'
rewrites are already applied. It appends the filtered `Plain` text to
`_tvls_pipeline_delivered` and marks the result object with
`_tvls_confirmed` (dataclass, no slots — **do not use `id()` as a token**).

`TextVoiceLangSplit.delivered_voice_text(event)` is the single authority for
"what would a voice say": the accumulated shipped text, and only when that is
empty, `_send_message_to_user_current_session_plain_texts`. **Never concatenate
the two** — the user received them as separate messages.

### Turn state (event extras, reset in `on_llm_request`)

| Extra | Set by | Purpose |
|-------|--------|---------|
| `_tvls_decorated` | first text-bearing decoration | later steps must not re-voice or re-translate |
| `_tvls_pipeline_delivered` | `after_message_sent` | the text the user actually received |
| `_tvls_final_seen` | `on_llm_response` | the next delivered message is the turn's last |
| `_tvls_voice_requested` | `VoiceTool.call()` (accepted) | LLM asked for a voice |
| `_tvls_deferred_voice_sent` | the service / streaming wrapper | request consumed exactly once |

`on_llm_request` **resets** all five (a follow-up turn can reuse the event) and
logs a `WARNING` if the previous turn left a request unserved.

### Every refusal is logged

The service never ends silently: missing delivered text, `voice_gate_env`
failure, `voice_gate_text` failure (`empty` / `too_long` with both numbers),
translation/TTS failure and a partially-sent voice all log at `WARNING`.
A dropped voice is therefore always visible in the log.

### Invariants — do not break these

1. **One verdict function.** All consumers (tool, service, streaming follow-up,
   inline path) go through `voice_utils.voice_gate_text` /
   `voice_gate_env`. Never re-implement the emptiness/cap comparison.
2. **Tool mode never voices inline**, and every early return in tool mode sets
   `GENERAL_RESULT` + `use_t2i_ = False`. Otherwise the built-in TTS reads the
   reply a second time.
3. **Never call `event.clear_extra(<key>)`.** `AstrMessageEvent.clear_extra(self)`
   takes no argument (it wipes every extra); use `set_extra(key, None)`.
   v1.7.0–v1.12.0 got this wrong and the `TypeError` silently disabled the
   whole deferred-voice path.
4. **The voiced text is the delivered text, from one source.** Do not snapshot
   text at decoration time (it may be a prefix, or pre-sibling-cleanup) and do
   not merge `send_message_to_user` text into the shipped text.
5. **One request, at most one attempt.** Set `_tvls_deferred_voice_sent`
   before spawning, and only in the single owner of the mode.
6. **The plugin never rewrites user-visible content, with one narrow exception.**
   Message count belongs to AstrBot (`buffer_intermediate_messages`); a plugin
   cannot remove the pre-tool message. In tool mode with a requested voice, an
   all-plain second message that exactly repeats the last delivered text is
   suppressed (and the voice is serviced from the delivered text) — only when
   the voice environment and the turn-level gate both allow a voice. Media,
   different text and non-final messages are never touched. The duplicate
   assistant entry stays in conversation history; the plugin does not edit it.


## Streaming voice mechanism (v1.5.0+)

`on_llm_request` runs BEFORE agent. It monkey-patches `event.send_streaming`:

```python
async def _patched(stream, *args, **kwargs):
    stream_ok = False
    try:
        await original(stream, *args, **kwargs)
        stream_ok = True
    finally:
        await voice_utils.send_streaming_follow_up(..., stream_ok=stream_ok)
```

Guarded by `_tvls_stream_patched` to prevent double-patching. The wrapper is the
single streaming voice owner: it consumes `_tvls_deferred_voice_sent` **only
after a successful send**, warns on a partial send, and skips the voice entirely
when `stream_ok` is False (the user never received the text).

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

FishAudio S2 uses natural-language `[bracket]` cues. English words are recognized; Japanese words (`[嬉しい]`) are spoken as text.

### Modes and the cue-free default (v1.11.0)

`emotion_intensity` selects a **markup syntax family**, not an intensity ramp. The default is `universal_safe`, which emits **no markup at all**, because the plugin cannot know whether a user's TTS provider interprets cue markup or reads it aloud. `auto`, `expressive`, and `restrained` keep their pre-v1.11.0 text byte-for-byte for Fish Audio S2 users; `drama3` is new for the Drama 3 model.

| Mode | Markup emitted | Intended TTS |
|------|----------------|--------------|
| `universal_safe` (default) | none | any provider |
| `drama3` | `[free-form direction]`, source-grounded `[sound]`, paired `<whisper>…</whisper>` | Fish Audio Drama 3 |
| `auto` / `expressive` / `restrained` | 24-tag S2 emotion list | Fish Audio S2 family |

`prompts._normalize_mode` is the single source of truth. **Unknown, empty, or malformed values fall back to `universal_safe`, never to a cue-emitting mode** — a broken config must not silently start writing markup into speech. `translate.py` passes `"universal_safe"` as its `config.get` default so a config missing the key agrees with the schema.

`_EMOTION_POLICIES` and `_EMOTION_SYSTEM_LINES` are parallel dicts: every key must exist in both, and `tests/test_translation_prompt.py::test_all_declared_modes_dispatch` enforces one distinct policy block per mode.

### What each design choice protects

- **`universal_safe` has no `[` or `<` anywhere in its text.** The test asserts this literally; keep it true when editing.
- **`drama3` keeps the phoneme ban and the no-nesting / no-stacking bans.** Only the sound-effect ban is intentionally relaxed.
- **Sound markers in `drama3` are strict: source-grounded only.** The model may modulate *how* a line is spoken but must not invent events or ambience. This is the `Never fabricate performance content` rule; do not soften it without the user asking.
- **Legacy modes are frozen.** `tests/test_split_parity.py` compares `_EMOTION_POLICY_RESTRAINED/_AUTO/_EXPRESSIVE` and the three legacy `_EMOTION_SYSTEM_LINES` entries against the pre-refactor implementation. New modes must be purely additive; `test_new_models_are_purely_additive` pins the added key set to exactly `{universal_safe, drama3}`.

### Paired inline tags vs. sentence splitting — CRITICAL

`text_utils.split_sentences` splits on sentence-end punctuation, and each chunk becomes a **separate TTS request**. Splitting inside `<whisper>…</whisper>` would separate the halves, losing the effect or making the tag audible. Two mechanisms prevent that:

1. `_tag_spans` + `_text_mask` mark every character inside a recognized paired tag as unbreakable, computed **once on the full text** so offsets stay valid across hard breaks (each break advances `start` into the same original string).
2. `_break_position` prefers a space before the cap, then the first usable space at any later position (a longer chunk beats a broken tag), then falls back to `limit` so the caller always makes progress.

Recognized tags: `whisper`, `emphasis`, `soft`, `fast`, `slow`, `stress`, `forceful`. A tag spanning a sentence end merges those sentences into one chunk by design. An unclosed tag is not special-cased — the splitter must never hang or drop text on malformed model output.

### Leading cue stripping — why position zero is special

`text_utils.strip_leading_cues` removes `[cue]` markup at the very start of a reply (including stacked cues like `[calm][whispering]`), because the prompt always places a cue immediately before the words it governs and never at position zero. A leading bracket is therefore a model violation, and removing it keeps a violating reply speakable on providers that accept no markup.

Rules that must hold:
- Only a **leading** cue is removed. A cue later in the text is deliberate semantics and must survive, because only the TTS model can interpret it.
- Paired `<tag>` markup is never touched here.
- A bracket that is data, not a cue (e.g. a list like `[1] 通过`), is not leading and must not be touched.
- This runs inside `filter_text_for_tts`, i.e. **before translation**, so it acts on the LLM's raw reply.

## Translation reference context (v1.12.0)

`translate_context_mode` adds optional persona / recent-conversation reference blocks to the translation prompt. Default `off` keeps the prompt byte-identical; `tests/test_split_parity.py` and `tests/test_translation_context.py` both lock that.

### Capture point — MUST stay in `on_llm_request`, BEFORE the stream-patch guard

`_capture_translation_context` snapshots `req.contexts` and resolves the persona inside `on_llm_request`:

```
build_main_agent → collect_initial_request (req.contexts = conversation.history)
                 → _decorate_llm_request → _ensure_persona_and_skills (persona applied)
OnLLMRequestEvent → plugin on_llm_request (capture here)
```

Because the snapshot is taken before the runner adds the current user turn, `req.contexts` is exactly the *previous* rounds. The capture call must stay before the `_tvls_stream_patched` early return: that guard fires on repeat calls, and every voice path (non-streaming / streaming follow-up / deferred voice) reads the snapshot from the same event object.

### Rules that must hold

- **`_no_save` records are excluded.** Core prepends persona `begin_dialogs` to `req.contexts` as `{"role": ..., "content": ..., "_no_save": True}` (`persona_mgr.py`); treating them as real history would push real rounds out of the window. system / tool / `_checkpoint` roles and non-text content are dropped too.
- **Persona resolution mirrors core inputs** — `umo`, `conversation.persona_id`, `platform_name`, `provider_settings` (only passed when the installed resolver accepts it, probed via `inspect.signature`). No conversation → no persona, exactly like core. WebChat special default (`resolve_selected_persona` returns persona `None`) falls back to `CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT` (imported defensively). There is no `get_default_persona_v3` fallback: substituting a persona the LLM never used is worse than injecting none.
- **Old AstrBot:** a missing `resolve_selected_persona` logs one warning and injects no persona; only `persona_context` can still inject history, `persona` injects nothing. Never pretend persona injection worked.
- **Third-party runners** (dify / coze / dashscope) build a bare `ProviderRequest` with no `conversation` and never populate `contexts` (`third_party.py`), so they inject nothing at all — plain translation. Only the local Agent Runner has local history.
- **Writes are additive:** an empty later capture never clobbers a good snapshot.
- **Fail-closed config:** unknown/empty/mistyped `translate_context_mode` → `off`; invalid rounds → 5 (clamped to 20); invalid budget → 2000 (`0` = unlimited, a positive value below `MIN_CONTEXT_CHARS=200` is raised to 200 so the tail-truncation fallback cannot emit a mid-word fragment). A broken config must never start sending persona/chat content to the translation provider.
- **Budget:** the rendered body never exceeds `translate_context_max_chars`; oldest lines are dropped first, then the tail of a single oversized message is kept.
- **Empty blocks must keep the legacy bytes.** `build_translation_prompt(..., persona_block="", context_block="")` and `build_translation_system_prompt(line)` are byte-identical to v1.11.0; the guard sentence and the extra system-prompt line appear only when a block is non-empty.

### Pure-module boundary

`context_utils.py` imports only `inspect` and `prompts` — no AstrBot — so the formatting rules are testable with plain `python`. AstrBot-facing capture lives in `main.py`; `translate.py` only calls `build_reference_blocks(config, event)` and swallows failures with a debug log.

### Anti-leak posture

The blocks are framed as reference data, and a guard sentence immediately before `Source text:` restates it. This is prompt-level only — it cannot be proven locally without a live provider. Do not claim it is verified; treat a leaked or answered context as a known residual risk.

## `GENERAL_RESULT` side effects

Setting `result.result_content_type = ResultContentType.GENERAL_RESULT`:
- ✅ Blocks built-in TTS (`is_llm_result()` returns False)
- ❌ Breaks segmented replies (`is_model_result()` returns False)
- ❌ RespondStage extracts and sends `Record` components first → voice arrives before text

In tool mode this flag is set on **every** message the plugin decorates (not
only the over-cap ones), because the plugin's voice is delivered later as its
own message. So segmented replies / splitter processing / plugins that read
`is_model_result()` are affected for every tool-mode reply — see the README
caveat.

**Decision:** Segmented replies are documented as mutually exclusive with this plugin. Users choose one or the other.

## Cap semantics

`tts_max_chars` is measured on the **turn's accumulated delivered speakable
text** (`voice_gate_text`), not per message: the voice speaks what the user
received in this turn, and a multi-step tool turn adds up. This is a deliberate
change from the pre-v1.13 per-message check; the schema hint, README and
CHANGELOG all say so. "Delivered" excludes text the plugin cannot see
(`tool_direct_result`, platform drops, `forward_threshold` merged forwards) and
tool-status messages sent by core inside the agent loop.

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

## Tool registration pattern

```python
@dataclass
class VoiceTool(FunctionTool[AstrAgentContext]):
    plugin: Any | None = None
    name: str = "tvls_send_voice"
    description: str = ""          # filled by build_description(max_chars)
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {}, "required": [],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        event = context.context.event   # AstrMessageEvent via agent context
        # gate → record the request (never promise delivery)
```

Register in `initialize()` via `self.context.add_llm_tools(tool_instance)`;
`initialize()` also assigns `tool.description = build_description(cap)` because
AstrBot reads `description`/`active` when it builds the request, so mutating
after registration is effective. Toggle `tool.active` to show/hide from the LLM.

## Config schema gotcha

`_conf_schema.json` types: `int`, `float`, `bool`, `string`, `text`, `list`, `file`, `object`, `template_list`. `"integer"` → `TypeError`.

**Dropdown selects:** a `string` field with an `options` array renders as a dropdown. `labels` is an optional parallel array — the dashboard shows `labels[i]` and stores `options[i]` (`ConfigItemRenderer.vue: getSelectItems`, indexes aligned, falls back to the raw option when a label is missing). Neither array is validated by the backend, so a typo'd option silently becomes an unselectable value that resolves to the fallback mode.

`tests/test_translation_prompt.py` guards this: `test_schema_offers_exactly_the_supported_modes` pins the option set to the implemented modes, the default to the first entry, and the label/option lengths; `test_only_the_default_mode_is_cue_free` enforces the schema's promise that only the first option emits no markup.

## Tests

Nine test suites, all in `tests/` (gitignored). They must run against the
**production** modules — v1.13.0 rewrote `tests/test_voice_tool.py` and
`tests/test_smoke.py` because the old harness copied the tool implementation and
used a synchronous `MagicMock` for `get_audio`, so a swallowed `TypeError` (and
the live `clear_extra(key)` bug) showed up as `PASS`.

Rules for this suite:
- never re-implement `VoiceTool`/`build_description` in a test; import them;
- mock events must mirror the real signatures (`clear_extra(self)` takes no key,
  `set_extra(key, value)`, `get_extra(key=None, default=None)`) — asserted with
  `inspect.signature`;
- `get_audio` is an `AsyncMock` returning a real temp path;
- "a voice was sent" is asserted by counting `context.send_message` awaits, not
  by checking a flag;
- runners end with `raise SystemExit(1 if failed else 0)`, so a failure is never
  silent.

```bash
# Run with AstrBot venv (REAL import layer, full integration):
$py = "D:\AstrBotLauncher-0.1.5.5\AstrBot\venv\Scripts\python.exe"
foreach ($t in @("test_translation_context.py", "test_split_parity.py",
                 "test_translation_prompt.py", "test_phase2.py", "test_smoke.py",
                 "test_split_and_toggle.py", "test_voice_tool.py",
                 "test_filter_text_for_tts.py", "test_strip_thinking.py")) {
  & $py "tests\$t"
}

# Standalone (no AstrBot deps needed, reference layer only):
python tests/test_translation_context.py
python tests/test_strip_thinking.py
python tests/test_filter_text_for_tts.py
```

`test_translation_prompt.py`, `test_translation_context.py` and
`test_strip_thinking.py` have dual layers: reference layer (standalone) +
integration layer (requires the AstrBot venv). `test_split_parity.py` needs the
plugin's `v1.8.0` git tag and rewrites `tests/_ref_main_v180.py`.

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
