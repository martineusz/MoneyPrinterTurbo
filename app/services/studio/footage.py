"""Story mode background: pick one continuous, not-recently-used segment of a footage file."""

import json
import os
import random

from loguru import logger

from . import ffmpeg_util

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
LOG_NAME = "footage_usage.json"


class FootageError(RuntimeError):
    pass


def free_intervals(duration: float, used: list[list[float]]) -> list[tuple[float, float]]:
    """Parts of [0, duration] not covered by any used range."""
    free, cursor = [], 0.0
    for start, end in sorted((max(0.0, a), min(duration, b)) for a, b in used if b > a):
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        free.append((cursor, duration))
    return free


def choose(durations: dict[str, float], used: dict[str, list[list[float]]], needed: float,
           rng: random.Random | None = None) -> tuple[str, float, bool]:
    """Pick (file, start, reused). ``reused`` is True when the footage is exhausted.

    A start is drawn uniformly over every position whose [start, start+needed] window lies in
    unused footage (so longer free stretches are proportionally more likely). When nothing
    unused remains, any position of any long-enough file is allowed again.
    """
    rng = rng or random.Random()
    long_enough = {n: d for n, d in durations.items() if d >= needed}
    if not long_enough:
        longest = max(durations.values(), default=0)
        raise FootageError(
            f"no footage file is long enough: need {needed:.1f}s, longest is {longest:.1f}s")

    def windows(use_log: bool):
        out = []
        for name, duration in long_enough.items():
            ranges = used.get(name, []) if use_log else []
            for lo, hi in free_intervals(duration, ranges):
                if hi - lo >= needed:
                    out.append((name, lo, hi - needed))
        return out

    options, reused = windows(True), False
    if not options:
        options, reused = windows(False), True
    total = sum(max(hi - lo, 1e-6) for _, lo, hi in options)
    pick = rng.random() * total
    for name, lo, hi in options:
        span = max(hi - lo, 1e-6)
        if pick <= span:
            return name, round(lo + min(pick, hi - lo), 3), reused
        pick -= span
    name, lo, hi = options[-1]
    return name, round(hi, 3), reused


def _read_log(folder: str) -> dict:
    try:
        with open(os.path.join(folder, LOG_NAME), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_log(folder: str, data: dict) -> None:
    path = os.path.join(folder, LOG_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def list_footage(folder: str) -> list[str]:
    if not folder or not os.path.isdir(folder):
        raise FootageError(f"footage folder not found: {folder!r}")
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXTENSIONS))
    if not files:
        raise FootageError(f"no video files ({', '.join(VIDEO_EXTENSIONS)}) in {folder}")
    return files


def pick_segment(folder: str, needed: float, rng: random.Random | None = None) -> tuple[str, float]:
    """Choose a segment, record it in the usage log, and return (absolute path, start)."""
    files = list_footage(folder)
    durations = {}
    for name in files:
        try:
            durations[name] = ffmpeg_util.probe(os.path.join(folder, name)).duration
        except ffmpeg_util.FFmpegError:
            logger.warning(f"studio: skipping unreadable footage file {name}")
    log = _read_log(folder)
    used = {k: v for k, v in log.items() if isinstance(v, list)}
    name, start, reused = choose(durations, used, needed, rng)
    if reused:
        logger.warning(
            "studio: all footage has been used; reusing ranges (usage log is reset). "
            "Add more footage to avoid repeats.")
        used = {}
    used.setdefault(name, []).append([start, round(start + needed, 3)])
    _write_log(folder, used)
    return os.path.join(folder, name), start
