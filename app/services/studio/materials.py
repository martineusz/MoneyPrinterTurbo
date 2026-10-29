"""Glue between MPT's materials/combine stages and the studio scene/story modes."""

import os
import re
import shutil

from loguru import logger


def prepare(task_id, params, audio_duration, fail):
    """Build the pre-rendered visuals for a studio source. Returns [path] or None on failure."""
    try:
        if params.video_source == "studio_scenes":
            from . import scenes

            return scenes.build_for_task(task_id, params, audio_duration)
        from . import story

        return story.build_for_task(task_id, params, audio_duration)
    except Exception as exc:
        logger.exception(f"studio: preparing materials failed: {exc}")
        fail(task_id, "materials", f"{type(exc).__name__}: {exc}")
        return None


def combine_function(params):
    """Stand-in for video.combine_videos: the visuals are already assembled, copy them."""

    def combine(combined_video_path, video_paths, audio_file=None, **_ignored):
        match = re.search(r"combined-(\d+)\.mp4$", os.path.basename(combined_video_path))
        position = int(match.group(1)) - 1 if match else 0
        source = video_paths[position] if position < len(video_paths) else video_paths[0]
        if os.path.abspath(source) != os.path.abspath(combined_video_path):
            shutil.copyfile(source, combined_video_path)
        return combined_video_path

    return combine
