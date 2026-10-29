"""Word-level captions: transcribe (faster-whisper, optional), align to the script, chunk, ASS."""

import difflib
import re
import unicodedata
from dataclasses import dataclass

from loguru import logger

from . import ass
from .srt import Cue

SENTENCE_END = re.compile(r"[.!?…。！？]+[\"')\]]*$")
MIN_WORD_SECONDS = 0.04


@dataclass
class Word:
    text: str
    start: float
    end: float


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _normalize(word: str) -> str:
    word = unicodedata.normalize("NFKC", word).casefold()
    return "".join(ch for ch in word if ch.isalnum())


def transcribe(audio_file: str, model_size: str = "small", device: str = "auto",
               language: str | None = None) -> list[Word]:
    """Word timestamps via faster-whisper. Raises ImportError when it is not installed."""
    from faster_whisper import WhisperModel

    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    logger.info(f"studio: transcribing with faster-whisper ({model_size}, {device})")
    model = WhisperModel(model_size, device=device, compute_type=compute)
    segments, _ = model.transcribe(audio_file, word_timestamps=True, language=language or None)
    return [Word(w.word.strip(), float(w.start), float(w.end))
            for seg in segments for w in (seg.words or []) if w.word.strip()]


def split_script_words(script: str) -> list[str]:
    return [w for w in script.split() if _normalize(w)]


def align(script_words: list[str], heard: list[Word], audio_duration: float) -> list[Word]:
    """Give every script word a time, using recognised words as anchors.

    The script text wins (misrecognitions are corrected); recognised timings supply the
    clock. Words the recogniser missed or garbled are spread proportionally (by length)
    over the time window between their nearest anchors.
    """
    n = len(script_words)
    if n == 0:
        return []
    keys = [_normalize(w) for w in script_words]
    heard_keys = [_normalize(w.text) for w in heard]
    times: list[tuple[float, float] | None] = [None] * n
    pending: list[tuple[int, int, float | None, float | None]] = []
    matcher = difflib.SequenceMatcher(a=keys, b=heard_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (heard[j1 + k].start, heard[j1 + k].end)
        elif tag == "replace":
            pending.append((i1, i2, heard[j1].start, heard[j2 - 1].end))
        elif tag == "delete":
            pending.append((i1, i2, None, None))
    for i1, i2, lo, hi in pending:
        if lo is None:
            prev = next((times[k] for k in range(i1 - 1, -1, -1) if times[k]), None)
            lo = prev[1] if prev else 0.0
        if hi is None:
            nxt = next((times[k] for k in range(i2, n) if times[k]), None)
            hi = nxt[0] if nxt else audio_duration
        hi = max(hi, lo + MIN_WORD_SECONDS * (i2 - i1))
        weights = [max(1, len(keys[k])) for k in range(i1, i2)]
        total, cursor = sum(weights), lo
        for k, weight in zip(range(i1, i2), weights):
            span = (hi - lo) * weight / total
            times[k] = (cursor, cursor + span)
            cursor += span
    words, floor = [], 0.0
    for text, span in zip(script_words, times):
        start, end = span
        start = max(start, floor)
        end = min(max(end, start + MIN_WORD_SECONDS), max(audio_duration, start + MIN_WORD_SECONDS))
        words.append(Word(text, round(start, 3), round(end, 3)))
        floor = end
    return words


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 24) -> list[list[Word]]:
    """Group words into chunks of 1..max_words, never crossing a sentence end."""
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        projected = sum(len(w.text) for w in current) + len(current) + len(word.text)
        if current and (len(current) >= max_words or projected > max_chars):
            chunks.append(current)
            current = []
        current.append(word)
        if SENTENCE_END.search(word.text):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def lines_to_words(cues: list[Cue]) -> list[Word]:
    """Fallback without speech recognition: spread each subtitle line over its own words."""
    words: list[Word] = []
    for cue in cues:
        tokens = cue.text.split()
        if not tokens:
            continue
        weights = [max(1, len(t)) for t in tokens]
        total, cursor = sum(weights), cue.start
        for token, weight in zip(tokens, weights):
            span = (cue.end - cue.start) * weight / total
            words.append(Word(token, round(cursor, 3), round(cursor + span, 3)))
            cursor += span
    return words


def build_caption_ass(words: list[Word], width: int, height: int, style: dict,
                      offset: float = 0.0, end_limit: float | None = None) -> str:
    """Big centred captions, 1-3 words at a time, the spoken word in the highlight color."""
    base = style.get("color", "#FFFFFF")
    highlight = style.get("highlight_color", "#FFD400")
    upper = bool(style.get("uppercase", True))
    chunks = chunk_words(words, int(style.get("max_words", 3)), int(style.get("max_chars", 24)))
    head = ass.header(width, height, {
        "font": style.get("font", "Arial"), "size": style.get("size", 110),
        "color": base, "outline_color": style.get("outline_color", "#000000"),
        "outline_width": style.get("outline_width", 6), "shadow": style.get("shadow", 0),
        "position": style.get("position", "middle"), "margin_v": style.get("margin_v", 0),
        "letter_spacing": style.get("letter_spacing", 0), "bold": True,
    })
    base_tag = ass.override_color(base)
    hi_tag = ass.override_color(highlight)
    events = []
    for chunk in chunks:
        for k, word in enumerate(chunk):
            start = word.start - offset
            end = (chunk[k + 1].start if k + 1 < len(chunk) else word.end) - offset
            if end_limit is not None:
                end = min(end, end_limit)
            if end <= 0 or end <= start:
                continue
            start = max(0.0, start)
            parts = []
            for j, other in enumerate(chunk):
                text = ass.escape_text(other.text, upper)
                parts.append(f"{{\\c{hi_tag}}}{text}{{\\c{base_tag}}}" if j == k else text)
            events.append(
                f"Dialogue: 0,{ass.ass_time(start)},{ass.ass_time(end)},Default,,0,0,0,,"
                f"{' '.join(parts)}\n")
    return head + "".join(events)
