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

# Paired inline delivery tags (Fish Audio Drama 3 word-level control).
# A split or hard break that lands inside one of these pairs separates the
# opening and closing halves into two independent TTS requests, which loses
# the effect or makes the tag itself audible.
_PAIRED_TAG_RE = re.compile(
    r"<(whisper|emphasis|soft|fast|slow|stress|forceful)>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _tag_spans(text: str) -> list[tuple[int, int]]:
    """Character spans covered by paired inline tags (empty when none)."""
    if "<" not in text:
        return []
    return [m.span() for m in _PAIRED_TAG_RE.finditer(text)]


def _text_mask(text: str, spans) -> list[bool]:
    """Per-character flag: True where a cut at this offset is allowed."""
    mask = [True] * len(text)
    for start, end in spans:
        for i in range(max(0, start), min(len(text), end)):
            mask[i] = False
    return mask


def _break_position(
    text: str, mask, limit: int, offset: int, back_window: int = 20
) -> int:
    """Hard-break position for a segment longer than the character cap.

    `text` is the buffer being split, `mask` covers the whole original text,
    and `offset` is where `text` starts inside it. A cut is unusable when it
    lands inside a paired inline tag, because the two halves would then be
    synthesized as separate requests.

    Search order:
    1. nearest space within `back_window` chars before `limit`;
    2. otherwise the first space outside every tag at any later position —
       preferring a longer chunk over one that breaks a tag open;
    3. otherwise, if the block contains spaces inside a tag, the first one
       (best effort: an oversized chunk is preferable to mangled text);
    4. otherwise the exact limit, so the caller always makes progress.
    """

    def usable(cut: int) -> bool:
        return cut > 0 and text[cut - 1].isspace() and mask[offset + cut - 1]

    for cut in range(limit, max(0, limit - back_window) - 1, -1):
        if usable(cut):
            return cut
    for cut in range(limit + 1, len(text)):
        if usable(cut):
            return cut
    for cut in range(1, len(text)):
        if text[cut - 1].isspace():
            return cut
    return limit


def split_sentences(text: str, max_chars: int = 200) -> list[str]:
    """Split translated text into sentence chunks for per-sentence TTS.

    - Splits after sentence-end punctuation (。！？!?…；;\\n), keeping the
      delimiter with its sentence (language-agnostic; CJK has no spaces).
      A period followed by a digit is NOT a sentence end ("3.14" stays whole).
    - A boundary that would fall inside a paired inline tag is not used, so
      ``<whisper>...</whisper>`` is never cut in two. When such a tag spans a
      sentence end, the surrounding sentences stay in one chunk.
    - ``max_chars`` > 0: a single sentence longer than the cap is hard-broken
      at the nearest safe space within 20 chars before the limit (fallback:
      first safe space after it, then the exact position), so every chunk
      stays within the provider-friendly bound.
    - ``max_chars`` == 0: sentence split only, no character cap.
    - Every sentence becomes its own voice chunk; short fragments naturally
      stay attached through the digit guard (no cross-sentence bundling).
    """
    if not text or not text.strip():
        return []
    # Tag spans and the safety mask are computed once on the full text;
    # splitting always works on a suffix of it, so offsets stay valid even
    # after hard breaks.
    mask = _text_mask(text, _tag_spans(text))
    boundaries: list[int] = []
    for match in _SENTENCE_END_RE.finditer(text):
        pos = match.end()
        if pos < len(mask) and not mask[pos]:
            continue
        boundaries.append(pos)
    boundaries.append(len(text))

    chunks: list[str] = []
    start = 0
    for pos in boundaries:
        while max_chars > 0 and pos - start > max_chars:
            cut = _break_position(text[start:pos], mask, max_chars, start)
            if cut <= 0:
                break
            piece = text[start : start + cut].strip()
            if piece:
                chunks.append(piece)
            start += cut
        piece = text[start:pos].strip()
        if piece:
            chunks.append(piece)
        start = pos
    return chunks
