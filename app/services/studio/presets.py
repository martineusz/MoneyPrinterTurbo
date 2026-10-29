"""Preset discovery and strict validation of style.json / post.json."""

import difflib
import json
import os
import re

from loguru import logger
from dataclasses import dataclass, field
from typing import Any

from . import config as studio_config


class PresetError(ValueError):
    """Raised with every problem found in a preset, one per line."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class F:
    kind: str  # str num int bool color file dir regex list obj strmap
    choices: tuple | None = None
    lo: float | None = None
    hi: float | None = None
    schema: dict | None = None  # for obj / items of list
    pattern: str | None = None  # full-match regex for str values
    hint: str = ""  # shown when the pattern does not match


COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")

POST_SCHEMA = {
    "look": F("obj", schema={
        "lut": F("file"),
        "contrast": F("num", lo=-1000, hi=1000),
        "brightness": F("num", lo=-1, hi=1),
        "saturation": F("num", lo=0, hi=3),
        "gamma": F("num", lo=0.1, hi=10),
        "blur": F("num", lo=0, hi=100),
        "chroma_offset": F("int", lo=0, hi=64),
        "vignette": F("num", lo=0, hi=1),
        "grain": F("num", lo=0, hi=100),
        # analog / VHS effects (all off by default)
        "scanlines": F("num", lo=0, hi=1),
        "chroma_bleed": F("num", lo=0, hi=64),
        "jitter": F("num", lo=0, hi=1),
        "tracking_bar": F("bool"),
        "fps": F("int", lo=1, hi=60),
        "frame_43": F("bool"),
        "timestamp": F("obj", schema={
            "format": F("str", pattern=r"[^'\\]+", hint="must not contain ' or backslash"),
            "start": F("str", pattern=r"random|\d{1,2}:[0-5]\d:[0-5]\d",
                       hint="use HH:MM:SS or 'random'"),
            "position": F("str", choices=("top_left", "bottom_right")),
            "font": F("file"),
            "size": F("int", lo=8, hi=300),
            "color": F("color"),
        }),
    }),
    "subtitles": F("obj", schema={
        "enabled": F("bool"),
        "font": F("str"),
        "fonts_dir": F("dir"),
        "size": F("int", lo=1, hi=500),
        "color": F("color"),
        "outline_color": F("color"),
        "outline_width": F("num", lo=0, hi=50),
        "shadow": F("num", lo=0, hi=50),
        "uppercase": F("bool"),
        "position": F("str", choices=("top", "middle", "bottom")),
        "margin_v": F("int", lo=0, hi=2000),
        "letter_spacing": F("num", lo=-20, hi=50),
        "under_grain": F("bool"),
    }),
    "voice_volume": F("num", lo=0, hi=10),
    "voice_fx": F("str"),
    "bgm": F("obj", schema={
        "file": F("file"),
        "volume": F("num", lo=0, hi=10),
        "fade_out": F("num", lo=0, hi=60),
    }),
    "cues": F("list", schema={
        "match": F("regex"),
        "at": F("num", lo=0),
        "once": F("bool"),
        "anchor": F("str", choices=("start", "end")),
        "offset": F("num", lo=-60, hi=60),
        "sfx": F("file"),
        "volume": F("num", lo=0, hi=10),
        "glitch": F("num", lo=0, hi=30),
        "glitch_style": F("str", choices=("shift",)),
        "invert": F("num", lo=0, hi=30),
        "flash": F("num", lo=0, hi=30),
        "black": F("num", lo=0, hi=30),
        "silence_before": F("num", lo=0, hi=30),
    }),
    "x264": F("obj", schema={
        "preset": F("str", choices=(
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower")),
        "crf": F("int", lo=0, hi=51),
    }),
}

STYLE_SCHEMA: dict = {
    "style_prefix": F("str"),
    "style_suffix": F("str"),
    "prompt_guidance": F("str"),
    "negative_prompt": F("str"),
    "entities": F("strmap"),
    "seed_mode": F("str", choices=("fixed", "per_scene", "random")),
    "base_seed": F("int", lo=0, hi=2**31 - 1),
    "width": F("int", lo=64, hi=8192),
    "height": F("int", lo=64, hi=8192),
    "comfyui_workflow": F("file"),
    "story": F("obj", schema={
        "footage_dir": F("dir"),
        "title_card": F("obj", schema={
            "background_color": F("color"),
            "opacity": F("num", lo=0, hi=1),
            "radius": F("int", lo=0, hi=200),
            "channel_name": F("str"),
            "avatar": F("file"),
            "font": F("file"),
            "title_size": F("int", lo=12, hi=300),
            "title_color": F("color"),
            "channel_size": F("int", lo=10, hi=200),
            "channel_color": F("color"),
            "label": F("str"),
            "label_color": F("color"),
            "label_background": F("color"),
        }),
        "captions": F("obj", schema={
            "font": F("str"),
            "fonts_dir": F("dir"),
            "size": F("int", lo=8, hi=500),
            "color": F("color"),
            "highlight_color": F("color"),
            "outline_color": F("color"),
            "outline_width": F("num", lo=0, hi=50),
            "shadow": F("num", lo=0, hi=50),
            "uppercase": F("bool"),
            "position": F("str", choices=("top", "middle", "bottom")),
            "margin_v": F("int", lo=0, hi=2000),
            "letter_spacing": F("num", lo=-20, hi=50),
            "max_words": F("int", lo=1, hi=6),
            "max_chars": F("int", lo=6, hi=60),
        }),
    }),
}


def register_style_schema(extra: dict) -> None:
    """Let a feature module (e.g. story mode) add its own style.json keys."""
    STYLE_SCHEMA.update(extra)


_TYPE_NAMES = {
    "str": "a string", "num": "a number", "int": "an integer", "bool": "true/false",
    "color": "a #RRGGBB color", "file": "a file path", "dir": "a folder path",
    "regex": "a regular expression", "list": "a list", "obj": "an object",
    "strmap": "an object of strings",
}


def _check(value: Any, f: F, where: str, base: str, problems: list[str]) -> Any:
    kind = f.kind

    def bad(why: str | None = None):
        problems.append(f"{where}: {why or 'expected ' + _TYPE_NAMES[kind] + ', got ' + repr(value)}")

    if kind in ("str", "color", "file", "dir", "regex"):
        if not isinstance(value, str):
            return bad()
        if kind == "color" and not COLOR_RE.fullmatch(value):
            return bad(f"invalid color {value!r}; expected #RRGGBB")
        if kind == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                return bad(f"invalid regex {value!r}: {exc}")
        if f.choices and value not in f.choices:
            return bad(f"must be one of {', '.join(f.choices)}; got {value!r}")
        if f.pattern and not re.fullmatch(f.pattern, value):
            return bad(f"invalid value {value!r}" + (f": {f.hint}" if f.hint else ""))
        if kind in ("file", "dir"):
            full = value if os.path.isabs(value) else os.path.join(base, value)
            ok = os.path.isfile(full) if kind == "file" else os.path.isdir(full)
            if not ok:
                return bad(f"{'file' if kind == 'file' else 'folder'} not found: {full}")
            return os.path.normpath(full)
        return value
    if kind in ("num", "int"):
        types = (int, float) if kind == "num" else (int,)
        if isinstance(value, bool) or not isinstance(value, types):
            return bad()
        if (f.lo is not None and value < f.lo) or (f.hi is not None and value > f.hi):
            return bad(f"{value} out of range [{f.lo}, {f.hi}]")
        return value
    if kind == "bool":
        return value if isinstance(value, bool) else bad()
    if kind == "list":
        if not isinstance(value, list):
            return bad()
        return [_check(v, F("obj", schema=f.schema), f"{where}[{i}]", base, problems)
                for i, v in enumerate(value)]
    if kind == "strmap":
        if not isinstance(value, dict):
            return bad()
        for k, v in value.items():
            if not isinstance(v, str):
                problems.append(f"{where}.{k}: expected a string, got {v!r}")
        return value
    if kind == "obj":
        if not isinstance(value, dict):
            return bad()
        return validate_object(value, f.schema or {}, where, base, problems)
    return value


def validate_object(data: dict, schema: dict, where: str, base: str, problems: list[str]) -> dict:
    out = {}
    for key, value in data.items():
        if key not in schema:
            hint = difflib.get_close_matches(key, schema, n=1)
            problems.append(
                f"{where}: unknown key '{key}'" + (f" (did you mean '{hint[0]}'?)" if hint else "")
                + f"; allowed: {', '.join(sorted(schema))}"
            )
            continue
        out[key] = _check(value, schema[key], f"{where}.{key}", base, problems)
    return out


def _load_json(path: str, problems: list[str]) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        problems.append(f"{path}: invalid JSON ({exc})")
        return None
    except OSError as exc:
        problems.append(f"{path}: cannot read ({exc})")
        return None
    if not isinstance(data, dict):
        problems.append(f"{path}: top level must be an object")
        return None
    return data


def validate_cues(cues: list, where: str, problems: list[str]) -> None:
    effects = ("sfx", "glitch", "invert", "flash", "black", "silence_before")
    for i, cue in enumerate(cues):
        if not isinstance(cue, dict):
            continue
        tag = f"{where}.cues[{i}]"
        if ("match" in cue) == ("at" in cue):
            problems.append(f"{tag}: exactly one of 'match' or 'at' is required")
        if not any(cue.get(e) for e in effects):
            problems.append(f"{tag}: cue has no effect (use one of {', '.join(effects)})")
        if "at" in cue and ("anchor" in cue or "once" in cue):
            problems.append(f"{tag}: 'anchor'/'once' only apply to 'match' cues")


_warned_frame43: set = set()


def frame43_style_warning(preset: "Preset") -> str:
    """Warning text when look.frame_43 is on but style.json is not sized for a 4:3 picture."""
    if not (preset.post or {}).get("look", {}).get("frame_43") or preset.style is None:
        return ""
    width, height = preset.style.get("width"), preset.style.get("height")
    if width and height and abs(width / height - 4 / 3) <= 0.1:
        return ""
    size = f"{width}x{height}" if width and height else "unset (defaults to the portrait video size)"
    return (
        f"preset '{preset.name}': look.frame_43 is on but style.json width/height is {size}, "
        "not 4:3 landscape. Scene mode fills the 4:3 box with each image, so a portrait image "
        "gets center-cropped. Try width/height of 1152x864 or 1024x768."
    )


@dataclass
class Preset:
    name: str
    root: str
    style: dict | None = None
    post: dict | None = None
    problems: list[str] = field(default_factory=list)


def preset_path(name: str, cfg: dict | None = None) -> str:
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise PresetError([f"invalid preset name {name!r}"])
    return os.path.join(studio_config.presets_dir(cfg), name)


def list_presets(cfg: dict | None = None) -> list[str]:
    root = studio_config.presets_dir(cfg)
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def load(name: str, cfg: dict | None = None) -> Preset:
    """Load and validate a preset. Raises PresetError listing every problem."""
    root = preset_path(name, cfg)
    if not os.path.isdir(root):
        raise PresetError([f"preset folder not found: {root}"])
    problems: list[str] = []
    preset = Preset(name=name, root=root)
    post_file = os.path.join(root, "post.json")
    if os.path.isfile(post_file) and (raw := _load_json(post_file, problems)) is not None:
        preset.post = validate_object(raw, POST_SCHEMA, "post.json", root, problems)
        cues = raw.get("cues")
        validate_cues(cues if isinstance(cues, list) else [], "post.json", problems)
    style_file = os.path.join(root, "style.json")
    if os.path.isfile(style_file) and (raw := _load_json(style_file, problems)) is not None:
        preset.style = validate_object(raw, STYLE_SCHEMA, "style.json", root, problems)
    if problems:
        raise PresetError([f"[{name}] {p}" for p in problems])
    if (warning := frame43_style_warning(preset)) and warning not in _warned_frame43:
        _warned_frame43.add(warning)  # presets are loaded several times per task
        logger.warning(f"studio: {warning}")
    return preset
