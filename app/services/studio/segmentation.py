"""Split a subtitle timeline into scenes that exactly tile the audio."""

import math
from dataclasses import dataclass

from .srt import Cue


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _r(value: float) -> float:
    return round(value, 3)


def _split_text(text: str, parts: int) -> list[str]:
    """Split on word boundaries into ``parts`` chunks of similar length."""
    words = text.split()
    if len(words) < parts:
        # CJK or very short text: split by characters instead
        chunk = math.ceil(len(text) / parts)
        out = [text[i:i + chunk] for i in range(0, len(text), chunk)]
        return (out + [""] * parts)[:parts]
    per = len(words) / parts
    return [" ".join(words[round(i * per):round((i + 1) * per)]) for i in range(parts)]


def segment(cues: list[Cue], audio_duration: float, min_seconds: float = 2.0,
            max_seconds: float = 5.0) -> list[Segment]:
    """Build scenes from subtitle cues.

    1. Tile: cue i spans [start_i, start_{i+1}); the first starts at 0 and the last ends at
       the audio end, so gaps in the SRT never leave holes in the timeline.
    2. Merge: a scene shorter than ``min_seconds`` joins the next one (the last one joins
       the previous).
    3. Split: a scene longer than ``max_seconds`` becomes several equal slices.
    """
    if audio_duration <= 0:
        raise ValueError("audio duration must be positive")
    if min_seconds <= 0 or max_seconds <= 0 or min_seconds > max_seconds:
        raise ValueError("need 0 < min_scene_seconds <= max_scene_seconds")
    usable = [c for c in sorted(cues, key=lambda c: c.start) if c.start < audio_duration]
    if not usable:
        raise ValueError("no subtitle lines inside the audio duration")

    tiled: list[Segment] = []
    for i, cue in enumerate(usable):
        start = 0.0 if i == 0 else cue.start
        end = usable[i + 1].start if i + 1 < len(usable) else audio_duration
        if end > start:
            tiled.append(Segment(_r(start), _r(end), " ".join(cue.text.split())))
    tiled[-1].end = _r(audio_duration)

    merged: list[Segment] = []
    pending: Segment | None = None
    for seg in tiled:
        if pending is not None:
            seg = Segment(pending.start, seg.end, f"{pending.text} {seg.text}")
            pending = None
        if seg.end - seg.start < min_seconds:
            pending = seg
        else:
            merged.append(seg)
    if pending is not None:
        if merged:
            last = merged[-1]
            merged[-1] = Segment(last.start, pending.end, f"{last.text} {pending.text}")
        else:
            merged.append(pending)  # whole video shorter than min: single scene

    result: list[Segment] = []
    for seg in merged:
        duration = seg.end - seg.start
        parts = max(1, math.ceil(round(duration / max_seconds, 6)))
        if parts == 1:
            result.append(seg)
            continue
        texts = _split_text(seg.text, parts)
        for k in range(parts):
            start = _r(seg.start + duration * k / parts)
            end = seg.end if k == parts - 1 else _r(seg.start + duration * (k + 1) / parts)
            result.append(Segment(start, end, texts[k] or seg.text))
    return result


def check_tiling(scenes: list[dict], audio_duration: float, tolerance: float = 0.002) -> None:
    """Raise ValueError unless scenes tile [0, audio_duration] with no gaps or overlaps."""
    if not scenes:
        raise ValueError("scenes.json has no scenes")
    expected = 0.0
    for scene in scenes:
        if abs(scene["start"] - expected) > tolerance:
            kind = "gap" if scene["start"] > expected else "overlap"
            raise ValueError(
                f"scene {scene['index']}: {kind} (starts at {scene['start']}, expected {expected})")
        if scene["end"] <= scene["start"]:
            raise ValueError(f"scene {scene['index']}: end must be after start")
        expected = scene["end"]
    if abs(expected - audio_duration) > tolerance:
        raise ValueError(
            f"scenes end at {expected} but the audio lasts {audio_duration}")
