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
