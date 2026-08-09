"""Text cleaning utilities for the TTS pipeline.

Standalone-testable (only depends on astrbot.api.logger).
Extracted verbatim from main.py (pre-v1.8.0 split).
"""

import re

from astrbot.api import logger


def compile_filter_patterns(patterns) -> list:
    """Compile user remove_patterns regexes, skipping invalid ones."""
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            logger.warning(f"[text_voice_lang_split] Invalid regex pattern: {p}")
    return compiled


def filter_text_for_tts(text: str, patterns) -> str:
    """Strip visual noise (markdown, URLs, code, user patterns) pre-translation."""
    if not text:
        return ""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://[a-zA-Z0-9./?#&=\-+%:!*'();,@[\]~_$]+", "", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    for pattern in patterns:
        text = pattern.sub("", text)
    return text


def strip_thinking(text: str) -> str:
    """Strip LLM reasoning artifacts from translation output.

    CRITICAL: the `\\s*response` pattern strips DeepSeek-R1's " response"
    separator and must NOT be changed to `\\s*\\presponse` — `\\p` is an
    invalid escape in Python 3.12 regex and silently breaks all translations.
    """
    if not text:
        return text
    had_thinking = bool(re.search(r"<think>", text))
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if had_thinking:
        text = re.sub(r"^.*?\s*response", "", text, flags=re.DOTALL)
    text = re.sub(r"</?think>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SENTENCE_END_RE = re.compile(r"(?<=[。！？.!?…；;])(?!\d)|(?<=\n)")


def _break_position(text: str, limit: int) -> int:
    """Hard-break position: nearest space within 20 chars before `limit`."""
    for i in range(limit - 1, max(0, limit - 20) - 1, -1):
        if text[i].isspace():
            return i + 1
    return limit


def split_sentences(text: str, max_chars: int = 200) -> list[str]:
    """Split translated text into sentence chunks for per-sentence TTS.

    - Splits after sentence-end punctuation (。！？!?…；;\\n), keeping the
      delimiter with its sentence (language-agnostic; CJK has no spaces).
      A period followed by a digit is NOT a sentence end ("3.14" stays whole).
    - ``max_chars`` > 0: a single sentence longer than the cap is hard-broken
      at the nearest space within 20 chars before the limit (fallback: exact
      position), so every chunk stays within the provider-friendly bound.
    - ``max_chars`` == 0: sentence split only, no character cap.
    - Every sentence becomes its own voice chunk; short fragments naturally
      stay attached through the digit guard (no cross-sentence bundling).
    """
    if not text or not text.strip():
        return []
    parts = [p for p in _SENTENCE_END_RE.split(text) if p.strip()]
    if not parts:
        return [text]
    if max_chars <= 0:
        return parts
    chunks: list[str] = []
    for part in parts:
        buf = part
        while len(buf) > max_chars:
            cut = _break_position(buf, max_chars)
            chunks.append(buf[:cut].strip())
            buf = buf[cut:]
        chunks.append(buf.strip())
    return chunks
