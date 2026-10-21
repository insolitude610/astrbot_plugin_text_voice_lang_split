"""Optional persona/context reference helpers for translation prompts.

Pure module: the standard library plus `prompts` only, so the formatting rules
can be tested without AstrBot installed. AstrBot-facing capture lives in
`main.py`; this module consumes already-captured data and never mutates state.
"""

import inspect

from . import prompts

CONTEXT_MODES = ("off", "persona", "persona_context")
DEFAULT_CONTEXT_MODE = "off"
DEFAULT_ROUNDS = 5
MAX_ROUNDS = 20
DEFAULT_MAX_CHARS = 2000
MIN_CONTEXT_CHARS = 200
"""Smallest effective budget: a tiny non-zero value would tail-slice a rendered
line into a mid-word fragment, which is prompt noise rather than context."""
MAX_SNAPSHOT_MESSAGES = 80

_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}


def normalize_context_mode(value) -> str:
    """Normalize a config value to a known mode, failing closed to "off".

    Unknown, empty, or malformed values must not start injecting persona or
    chat content into a request to the translation provider.
    """
    mode = value.strip().lower() if isinstance(value, str) else ""
    return mode if mode in CONTEXT_MODES else DEFAULT_CONTEXT_MODE


def _to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_rounds(value) -> int:
    """Clamp the configured round count to a usable positive integer."""
    rounds = _to_int(value)
    if rounds is None or rounds <= 0:
        return DEFAULT_ROUNDS
    return min(rounds, MAX_ROUNDS)


def normalize_max_chars(value) -> int:
    """Clamp the configured character budget; 0 means unlimited.

    A positive value below MIN_CONTEXT_CHARS is raised to that floor, because a
    tiny budget would only produce a truncated mid-word fragment.
    """
    max_chars = _to_int(value)
    if max_chars is None or max_chars < 0:
        return DEFAULT_MAX_CHARS
    if 0 < max_chars < MIN_CONTEXT_CHARS:
        return MIN_CONTEXT_CHARS
    return max_chars


def _config_get(config, key, default=None):
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    try:
        value = getter(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _event_extra(event, key, default=None):
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return default
    try:
        value = getter(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _content_text(content) -> str:
    """Flatten a history `content` field (str, or list of TextPart dicts)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return " ".join(parts)


def extract_recent_messages(contexts, limit: int = MAX_SNAPSHOT_MESSAGES):
    """Keep the newest speakable history turns as (role, text) pairs.

    Drops non user/assistant roles (system, tool, `_checkpoint`), records the
    pipeline marks as unsaved (persona `begin_dialogs` carry `_no_save`), and
    entries whose text is empty. The newest `limit` entries are kept.
    """
    if not isinstance(contexts, (list, tuple)):
        return []
    messages = []
    for record in contexts:
        if not isinstance(record, dict):
            continue
        if record.get("_no_save"):
            continue
        role = record.get("role")
        if role not in _ROLE_LABELS:
            continue
        text = _content_text(record.get("content")).strip()
        if not text:
            continue
        messages.append((role, text))
    if isinstance(limit, int) and limit > 0:
        return messages[-limit:]
    return messages


def _valid_message(message) -> bool:
    return (
        isinstance(message, (list, tuple))
        and len(message) == 2
        and message[0] in _ROLE_LABELS
        and isinstance(message[1], str)
    )


def select_recent_rounds(messages, rounds: int):
    """Take the last `rounds` user turns plus everything after them, in order.

    A round is counted by its user message, so multi-assistant (tool-call)
    turns do not inflate the count. The oldest kept round may start with an
    assistant message when the window begins mid-round.
    """
    selected = []
    user_turns = 0
    for message in reversed(list(messages or [])):
        if not _valid_message(message):
            continue
        role = message[0]
        if role == "user":
            if user_turns >= rounds:
                break
            user_turns += 1
        elif user_turns >= rounds:
            # An assistant message reached after the oldest kept round belongs
            # to an older round: stop instead of leaking a leading orphan.
            break
        selected.append((role, message[1]))
    selected.reverse()
    return selected


def render_context_lines(messages) -> list[str]:
    """Render turns as `User: ...` / `Assistant: ...` single-line entries."""
    lines = []
    for message in messages or []:
        if not _valid_message(message):
            continue
        collapsed = " ".join(message[1].split())
        if not collapsed:
            continue
        lines.append(f"{_ROLE_LABELS[message[0]]}: {collapsed}")
    return lines


def format_reference_context(messages, rounds: int, max_chars: int) -> str:
    """Render the reference context body within the character budget.

    Oldest lines are dropped first; a single over-budget message keeps its
    tail, because the most recent tone matters more than the beginning.
    """
    lines = render_context_lines(select_recent_rounds(messages, rounds))
    if max_chars > 0:
        while len(lines) > 1 and len("\n".join(lines)) > max_chars:
            lines.pop(0)
    body = "\n".join(lines)
    if max_chars > 0 and len(body) > max_chars:
        body = body[-max_chars:]
    return body


def _persona_prompt(persona) -> str:
    if not isinstance(persona, dict):
        return ""
    prompt = persona.get("prompt")
    if not isinstance(prompt, str):
        return ""
    return prompt.strip()


async def resolve_persona_prompt(
    context,
    event,
    conversation_persona_id,
    provider_settings=None,
    special_default_prompt: str = "",
) -> str:
    """Resolve the persona prompt AstrBot itself applied to this request.

    Mirrors the core call (`umo`, `conversation_persona_id`, `platform_name`,
    `provider_settings`) and the WebChat special-default branch. Any failure
    returns "" so translation degrades to its plain prompt.

    `provider_settings` is only passed when the installed resolver accepts it,
    so an older signature cannot raise. Missing persona API returns "".
    """
    manager = getattr(context, "persona_manager", None)
    resolver = getattr(manager, "resolve_selected_persona", None)
    if not callable(resolver):
        return ""

    platform_getter = getattr(event, "get_platform_name", None)
    platform_name = platform_getter() if callable(platform_getter) else ""
    kwargs = {
        "umo": getattr(event, "unified_msg_origin", None),
        "conversation_persona_id": conversation_persona_id,
        "platform_name": platform_name,
    }
    try:
        parameters = inspect.signature(resolver).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if "provider_settings" in parameters or accepts_kwargs:
        kwargs["provider_settings"] = provider_settings or {}

    try:
        _, persona, _, use_webchat_special_default = await resolver(**kwargs)
    except Exception:  # noqa: BLE001
        return ""

    prompt = _persona_prompt(persona)
    if prompt:
        return prompt
    if (
        use_webchat_special_default
        and special_default_prompt
        and _event_extra(event, "enable_default_system_prompt", True) is not False
    ):
        return special_default_prompt.strip()
    return ""


def build_reference_blocks(config, event) -> tuple[str, str]:
    """Build the (persona_block, context_block) pair for a translation call.

    "off" returns two empty strings without reading any event extra, so the
    default path is untouched.
    """
    mode = normalize_context_mode(
        _config_get(config, "translate_context_mode", DEFAULT_CONTEXT_MODE)
    )
    if mode == DEFAULT_CONTEXT_MODE:
        return "", ""

    persona_block = prompts.build_persona_block(
        _event_extra(event, "_tvls_persona_prompt", "")
    )
    context_block = ""
    if mode == "persona_context":
        body = format_reference_context(
            _event_extra(event, "_tvls_context_messages", []),
            normalize_rounds(
                _config_get(config, "translate_context_rounds", DEFAULT_ROUNDS)
            ),
            normalize_max_chars(
                _config_get(config, "translate_context_max_chars", DEFAULT_MAX_CHARS)
            ),
        )
        context_block = prompts.build_context_block(body)
    return persona_block, context_block
