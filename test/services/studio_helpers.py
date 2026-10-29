"""Shared helpers for studio tests (lavfi-generated media, no external files)."""

import json
import os
import subprocess

from app.services.studio import config as studio_config
from app.services.studio import ffmpeg_util

IDENTITY_CUBE = (
    "LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n"
)


def ff(*args):
    subprocess.run(
        [ffmpeg_util.get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def make_task(folder, video_seconds=6, audio_seconds=5, size="360x640", srt=None):
    os.makedirs(folder, exist_ok=True)
    ff("-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={video_seconds}", "-pix_fmt", "yuv420p",
       os.path.join(folder, "combined-1.mp4"))
    ff("-f", "lavfi", "-i", f"sine=frequency=330:duration={audio_seconds}",
       os.path.join(folder, "audio.mp3"))
    srt = srt or (
        "1\n00:00:00,000 --> 00:00:02,000\nHello {there}\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\nWait for it\n\n"
        "3\n00:00:03,500 --> 00:00:05,000\nThe end\n"
    )
    with open(os.path.join(folder, "subtitle.srt"), "w", encoding="utf-8") as handle:
        handle.write(srt)
    return folder


def make_preset(root, name, post, style=None, files=None):
    """Create presets/<name> under ``root`` and return the preset folder."""
    folder = os.path.join(root, name)
    os.makedirs(folder, exist_ok=True)
    if post is not None:
        with open(os.path.join(folder, "post.json"), "w", encoding="utf-8") as handle:
            json.dump(post, handle)
    if style is not None:
        with open(os.path.join(folder, "style.json"), "w", encoding="utf-8") as handle:
            json.dump(style, handle)
    for rel, content in (files or {}).items():
        target = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(target, mode) as handle:
            handle.write(content)
    return folder


def use_presets_dir(monkeypatch, root, **extra):
    monkeypatch.setattr(studio_config, "_raw", lambda: {"presets_dir": str(root), **extra})
