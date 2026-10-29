"""Studio: post-processing, scene mode and story mode for MoneyPrinterTurbo.

Upstream code only calls the thin functions below (see docs/STUDIO.md). Everything
is imported lazily so importing this package costs nothing and creates no cycles.
"""

import os

from loguru import logger

VIDEO_SOURCES = frozenset({"studio_scenes", "studio_story"})


def active_preset_name(params) -> str:
    """Preset requested for this task, or "" when studio is off (upstream behaviour)."""
    if not (_post_active(params) or params.video_source in VIDEO_SOURCES):
        return ""
    from . import config

    preset = getattr(params, "studio_preset", "")
    return (preset if isinstance(preset, str) and preset else config.get()["default_preset"])


def _post_active(params) -> bool:
    return getattr(params, "studio_post", False) is True


def preflight(params, stop_at: str) -> str:
    """Return an error message (fail the task before paid calls) or ""."""
    if stop_at not in ("materials", "video"):
        return ""
    name = active_preset_name(params)
    if not name:
        return ""
    from . import config, ffmpeg_util, presets

    try:
        cfg = config.get()
        preset = presets.load(name)
    except (presets.PresetError, config.StudioConfigError) as exc:
        return f"studio: {exc}"
    if params.video_source in VIDEO_SOURCES and stop_at == "video":
        problem = _source_preflight(params, cfg, preset)
        if problem:
            return f"studio: {problem}"
    if _post_active(params) and stop_at == "video":
        if preset.post is None:
            return f"studio: preset '{name}' has no post.json but post-processing is enabled"
        if preset.post.get("subtitles", {}).get("enabled", True) and not ffmpeg_util.has_libass():
            ffmpeg_util.require_libass()
            return "studio: the ffmpeg build has no libass; subtitles cannot be burned in"
    return ""


def _source_preflight(params, cfg, preset) -> str:
    if params.video_source == "studio_scenes":
        if not params.subtitle_enabled:
            return "scene mode builds scenes from subtitle.srt; enable subtitles"
        if cfg["image_backend"] == "comfyui":
            if not (preset.style or {}).get("comfyui_workflow"):
                return "image_backend = 'comfyui' needs comfyui_workflow in the preset's style.json"
        else:
            from app.config import config as app_config
            from app.services import material

            if not material.is_openai_image_enabled(dict(app_config.app)):
                return ("scene mode needs openai_image_base_url and openai_image_model in "
                        "config.toml (or set [studio] image_backend = 'comfyui')")
    return ""


def render_override(params, original):
    """Optionally replace MPT's MoviePy render when post-processing owns the final video."""
    if not _post_active(params):
        return original
    from . import config

    if not config.get()["skip_mpt_render"]:
        return original

    def mux_only(video_path, audio_path, subtitle_path, output_file, params, bgm_file_override=None):
        from . import ffmpeg_util

        logger.info("studio: skip_mpt_render is on; muxing video + narration only")
        tmp = output_file + ".tmp.mp4"
        ffmpeg_util.run(ffmpeg_util.base_cmd() + [
            "-i", video_path, "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", tmp])
        os.replace(tmp, output_file)
        return True

    return mux_only


def postprocess_final(task_id, params, index, combined_video_path, audio_file,
                      subtitle_path, final_video_path) -> None:
    """Post-process final-N.mp4 in place (original kept as final-N.raw.mp4)."""
    if not _post_active(params):
        return
    from . import post, presets

    task_folder = os.path.dirname(final_video_path)
    name = active_preset_name(params)
    try:
        preset = presets.load(name)
        if preset.post is None:
            logger.warning(f"studio: preset '{name}' has no post.json; keeping MPT output")
            return
        logger.info(
            "studio: post-processing owns subtitles and BGM; MPT's own subtitle burn-in "
            "and BGM are bypassed (original kept as final-%d.raw.mp4)" % index)
        post.apply_to_final(task_folder, preset, index)
    except Exception as exc:  # the raw MPT video is still a valid result
        logger.error(f"studio: post-processing failed, keeping MPT output: {exc}")


def combine_override(params, original):
    """Scene/story sources provide their own combined clip (Phase 2/3)."""
    if params.video_source not in VIDEO_SOURCES:
        return original
    from . import materials

    return materials.combine_function(params)


def prepare_materials(task_id, params, audio_duration, fail):
    from . import materials

    return materials.prepare(task_id, params, audio_duration, fail)


__all__ = [
    "VIDEO_SOURCES", "preflight", "render_override", "postprocess_final",
    "combine_override", "prepare_materials", "active_preset_name",
]
