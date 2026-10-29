"""Minimal, forgiving SRT parser."""

import re
from dataclasses import dataclass

_TIME = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


def _seconds(h, m, s, ms) -> float:
    # "5" after the separator means 500 ms, not 5 ms.
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            if m := _TIME.search(line):
                start = _seconds(*m.group(1, 2, 3, 4))
                end = _seconds(*m.group(5, 6, 7, 8))
                body = "\n".join(part.strip() for part in lines[i + 1:] if part.strip())
                if body and end > start:
                    cues.append(Cue(len(cues) + 1, start, end, body))
                break
    return cues


def read_srt(path: str) -> list[Cue]:
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return parse_srt(handle.read())


def format_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(cues: list[Cue]) -> str:
    return "".join(
        f"{i}\n{format_time(c.start)} --> {format_time(c.end)}\n{c.text}\n\n"
        for i, c in enumerate(cues, 1))


def clip_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Cues overlapping [start, end], shifted so the window starts at 0."""
    out = []
    for cue in cues:
        a, b = max(cue.start, start), min(cue.end, end)
        if b - a > 0.01:
            out.append(Cue(len(out) + 1, round(a - start, 3), round(b - start, 3), cue.text))
    return out
