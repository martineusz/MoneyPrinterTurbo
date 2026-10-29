"""Thin ffmpeg helpers shared by every studio module."""

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from loguru import logger


class FFmpegError(RuntimeError):
    pass


def get_ffmpeg() -> str:
    # Reuse MPT's resolver so studio and upstream always pick the same binary.
    from app.services import video

    return video.get_ffmpeg_binary()


@lru_cache(maxsize=8)
def _filters(ffmpeg: str) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        capture_output=True, text=True, errors="replace",
    )
    return result.stdout


def has_libass(ffmpeg: str | None = None) -> bool:
    ffmpeg = ffmpeg or get_ffmpeg()
    try:
        out = _filters(ffmpeg)
    except OSError:
        return False
    names = {parts[1] for parts in (line.split() for line in out.splitlines()) if len(parts) > 1}
    return "ass" in names and "subtitles" in names


def require_libass(ffmpeg: str | None = None) -> bool:
    """Log a clear, specific error when the ffmpeg build cannot burn subtitles."""
    ffmpeg = ffmpeg or get_ffmpeg()
    if has_libass(ffmpeg):
        return True
    logger.error(
        f"studio: ffmpeg at '{ffmpeg}' was built without libass (the 'ass'/'subtitles' "
        "filters are missing), so subtitles cannot be burned in. Install an ffmpeg build "
        "with --enable-libass (e.g. the gyan.dev 'full' or 'essentials' build) or set "
        "app.ffmpeg_path in config.toml."
    )
    return False


def quote_filter_value(value: str) -> str:
    """Escape a file path / free text for use as ONE filter option value.

    Two escaping layers apply (option parser, then filtergraph parser); wrapping
    the value in single quotes handles ``,`` ``;`` ``[`` ``]`` and spaces, so only
    backslashes, the drive-letter colon and apostrophes need care.
    Backslashes become forward slashes (ffmpeg accepts them on Windows), so a
    Windows path like ``C:`` + backslash + ``a b`` + backslash + ``x.cube`` turns
    into ``'C\\:/a b/x.cube'`` (the drive colon is escaped).
    """
    value = str(value).replace("\\", "/").replace(":", "\\:")
    value = value.replace("'", "'\\\\\\''")
    return f"'{value}'"


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})[\s,\[]")


def probe(path: str, ffmpeg: str | None = None) -> MediaInfo:
    ffmpeg = ffmpeg or get_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    info = MediaInfo()
    if m := _DURATION.search(result.stderr):
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := _VIDEO.search(result.stderr):
        info.width, info.height = int(m[1]), int(m[2])
    if not info.duration and not info.width:
        raise FFmpegError(f"cannot read media file {path}: {result.stderr.strip()[-300:]}")
    return info


def run(cmd: list[str], timeout: float | None = None) -> None:
    logger.debug("studio ffmpeg: " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({result.returncode}): {result.stderr.strip()[-1500:]}")


def base_cmd(ffmpeg: str | None = None) -> list[str]:
    return [ffmpeg or get_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
