"""The ``[studio]`` section of config.toml (read-only; upstream config code is untouched)."""

import os

DEFAULTS = {
    "presets_dir": "presets",
    "default_preset": "example",
    "skip_mpt_render": False,
    "x264_preset": "veryfast",
    "x264_crf": 18,
    "image_backend": "openai",
    "comfyui_url": "http://127.0.0.1:8188",
    "comfyui_timeout": 300,
    "min_scene_seconds": 2.0,
    "max_scene_seconds": 5.0,
    "scene_motions": ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static"],
    "scene_motion_order": "cycle",
    "footage_dir": "",
    "game_audio_volume": 0.0,
    "title_card_seconds": 3.0,
    "whisper_model": "small",
    "whisper_device": "auto",
    "max_part_seconds": 0,
}


class StudioConfigError(ValueError):
    pass


def _raw() -> dict:
    from app.config import config

    section = getattr(config, "_cfg", {}).get("studio", {})
    return section if isinstance(section, dict) else {}


def get(overrides: dict | None = None) -> dict:
    merged = dict(DEFAULTS)
    raw = {**_raw(), **(overrides or {})}
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise StudioConfigError(f"[studio] unknown keys: {', '.join(unknown)}")
    for key, value in raw.items():
        default = DEFAULTS[key]
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if not ok:
            raise StudioConfigError(
                f"[studio] {key} must be {type(default).__name__}, got {type(value).__name__}"
            )
        merged[key] = value
    if merged["image_backend"] not in ("openai", "comfyui"):
        raise StudioConfigError("[studio] image_backend must be 'openai' or 'comfyui'")
    if merged["scene_motion_order"] not in ("cycle", "random"):
        raise StudioConfigError("[studio] scene_motion_order must be 'cycle' or 'random'")
    return merged


def root_dir() -> str:
    from app.utils import utils

    return utils.root_dir()


def presets_dir(cfg: dict | None = None) -> str:
    path = (cfg or get())["presets_dir"]
    return path if os.path.isabs(path) else os.path.join(root_dir(), path)
