"""Scene mode: one generated image per narration segment, rendered in order.

scenes.json (in the task folder) is the editable source of truth. Edit ``prompt``,
``seed``, ``motion`` or the scene boundaries, then re-render:

    python -m app.services.studio.scenes <task_folder> [--regenerate 2,5] [--no-final]
"""

import argparse
import hashlib
import json
import os
import random
import sys

from . import config as studio_config
from . import ffmpeg_util, image_backends, motion, presets, scene_prompts
from .segmentation import check_tiling, segment
from .srt import read_srt

SCENES_FILE = "scenes.json"
COMBINED_NAME = "studio-combined.mp4"
SAFETY_MARGIN_FRAMES = 3  # mirrors video._VIDEO_DURATION_SAFETY_MARGIN (0.1 s at 30 fps)


class SceneError(RuntimeError):
    pass


# ---- scenes.json ---------------------------------------------------------------

def load_doc(task_folder: str) -> dict:
    path = os.path.join(task_folder, SCENES_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SceneError(f"cannot read {path}: {exc}") from exc
    validate_doc(doc)
    return doc


def save_doc(task_folder: str, doc: dict) -> str:
    path = os.path.join(task_folder, SCENES_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


REQUIRED = {"index": int, "start": (int, float), "end": (int, float), "narration": str,
            "prompt": str, "seed": int, "motion": str}


def validate_doc(doc: dict) -> None:
    if not isinstance(doc, dict) or not isinstance(doc.get("scenes"), list):
        raise SceneError("scenes.json must be an object with a 'scenes' list")
    for position, scene in enumerate(doc["scenes"], 1):
        for key, kind in REQUIRED.items():
            if not isinstance(scene.get(key), kind) or isinstance(scene.get(key), bool):
                raise SceneError(f"scene #{position}: '{key}' is missing or has the wrong type")
        if scene["motion"] not in motion.MOTIONS:
            raise SceneError(f"scene {scene['index']}: unknown motion '{scene['motion']}'")
        if not scene["prompt"].strip():
            raise SceneError(f"scene {scene['index']}: empty prompt")
    try:
        check_tiling(doc["scenes"], float(doc.get("audio_duration", 0)))
    except ValueError as exc:
        raise SceneError(str(exc)) from exc


# ---- building -----------------------------------------------------------------

def _seed_for(mode: str, base: int, index: int) -> int:
    if mode == "per_scene":
        return base + index
    if mode == "random":
        return random.SystemRandom().randrange(0, 2**31)
    return base


def _sizes(style: dict, aspect) -> tuple[tuple[int, int], tuple[int, int]]:
    """((render_w, render_h), (image_w, image_h)). Video size follows the aspect; the style's
    width/height is only the size requested from the image model (scaled up when rendering)."""
    from app.models.schema import VideoAspect

    render = VideoAspect(aspect).to_resolution()
    return render, (int(style.get("width", render[0])), int(style.get("height", render[1])))


def build_doc(script: str, cues, audio_duration: float, preset: presets.Preset, cfg: dict,
              aspect, ask=None) -> dict:
    style = preset.style or {}
    segments = segment(cues, audio_duration, cfg["min_scene_seconds"], cfg["max_scene_seconds"])
    prompts = scene_prompts.generate_prompts(
        script, [s.text for s in segments], style.get("entities", {}), ask=ask)
    mode, base = style.get("seed_mode", "fixed"), int(style.get("base_seed", 0))
    seeds = [_seed_for(mode, base, i) for i in range(1, len(segments) + 1)]
    motions = motion.pick_motions(
        len(segments), cfg["scene_motions"], cfg["scene_motion_order"], seeds)
    (width, height), (image_w, image_h) = _sizes(style, aspect)
    return {
        "version": 1, "preset": preset.name, "audio_duration": round(audio_duration, 3),
        "width": width, "height": height, "image_width": image_w, "image_height": image_h,
        "scenes": [
            {"index": i, "start": s.start, "end": s.end, "narration": s.text,
             "prompt": prompts[i - 1], "seed": seeds[i - 1], "image": "", "image_key": "",
             "motion": motions[i - 1]}
            for i, s in enumerate(segments, 1)
        ],
    }


# ---- rendering ----------------------------------------------------------------

def _full_prompt(scene: dict, style: dict) -> str:
    suffix = style.get("style_suffix", "").strip()
    return f"{scene['prompt'].strip()}, {suffix}" if suffix else scene["prompt"].strip()


def frame_counts(doc: dict) -> list[int]:
    """Frames per scene, from cumulative rounding so the clips tile the timeline exactly."""
    fps = motion.FPS
    counts = [round(s["end"] * fps) - round(s["start"] * fps) for s in doc["scenes"]]
    counts = [max(1, c) for c in counts]
    counts[-1] += SAFETY_MARGIN_FRAMES
    return counts


def render_clip(image: str, scene_motion: str, frames: int, width: int, height: int, out: str) -> None:
    tmp = out + ".tmp.mp4"
    cmd = ffmpeg_util.base_cmd() + [
        "-i", image, "-vf", motion.zoompan_filter(scene_motion, frames, width, height),
        "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(motion.FPS), tmp,
    ]
    ffmpeg_util.run(cmd)
    os.replace(tmp, out)


def _concat(clips: list[str], out: str) -> None:
    listing = out + ".txt"
    with open(listing, "w", encoding="utf-8") as handle:
        for clip in clips:
            safe = clip.replace("\\", "/").replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")
    tmp = out + ".tmp.mp4"
    try:
        ffmpeg_util.run(ffmpeg_util.base_cmd() + [
            "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy",
            "-movflags", "+faststart", tmp])
        os.replace(tmp, out)
    finally:
        for leftover in (listing, tmp):
            if os.path.exists(leftover):
                os.remove(leftover)


def render(task_folder: str, doc: dict, preset: presets.Preset, cfg: dict, backend,
           regenerate: set[int] | frozenset = frozenset(), image_dir: str | None = None,
           output: str | None = None) -> str:
    """Generate missing images, render every scene clip and concatenate them in order."""
    style = preset.style or {}
    negative = style.get("negative_prompt", "")
    width, height = doc["width"], doc["height"]
    image_w, image_h = doc.get("image_width", width), doc.get("image_height", height)
    clip_dir = os.path.join(task_folder, "scene_clips")
    os.makedirs(clip_dir, exist_ok=True)
    frames = frame_counts(doc)
    clips = []
    for scene, n_frames in zip(doc["scenes"], frames):
        try:
            path, key, _ = image_backends.fetch_image(
                backend, _full_prompt(scene, style), negative, scene["seed"], image_w, image_h,
                force=scene["index"] in regenerate, directory=image_dir)
        except image_backends.ImageBackendError as exc:
            save_doc(task_folder, doc)  # keep progress so a re-run only redoes what is missing
            raise SceneError(f"scene {scene['index']}: {exc}") from exc
        scene["image"], scene["image_key"] = path, key
        ident = hashlib.sha256(
            f"{key}|{scene['motion']}|{n_frames}|{width}x{height}".encode()).hexdigest()[:12]
        clip = os.path.join(clip_dir, f"{scene['index']:03d}-{ident}.mp4")
        if not os.path.isfile(clip):
            render_clip(path, scene["motion"], n_frames, width, height, clip)
        clips.append(clip)
    save_doc(task_folder, doc)
    out = output or os.path.join(task_folder, COMBINED_NAME)
    _concat(clips, out)
    # drop clips of scenes that were edited away
    keep = {os.path.basename(c) for c in clips}
    for name in os.listdir(clip_dir):
        if name.endswith(".mp4") and name not in keep:
            os.remove(os.path.join(clip_dir, name))
    return out


# ---- pipeline entry -----------------------------------------------------------

def build_for_task(task_id: str, params, audio_duration: float, ask=None, backend=None,
                   image_dir: str | None = None) -> list[str]:
    from app.utils import utils

    task_folder = utils.task_dir(task_id)
    cfg = studio_config.get()
    preset = presets.load(params.studio_preset or cfg["default_preset"], cfg)
    srt_path = os.path.join(task_folder, "subtitle.srt")
    if not os.path.isfile(srt_path):
        raise SceneError("scene mode needs subtitle.srt; enable subtitles (subtitle_enabled)")
    script = ""
    try:
        with open(os.path.join(task_folder, "script.json"), encoding="utf-8") as handle:
            script = json.load(handle).get("script", "")
    except (OSError, json.JSONDecodeError):
        pass
    cues = read_srt(srt_path)
    script = script or " ".join(c.text for c in cues)
    doc = build_doc(script, cues, audio_duration, preset, cfg, params.video_aspect, ask=ask)
    save_doc(task_folder, doc)
    backend = backend or image_backends.create_backend(cfg, preset.style, params.video_aspect)
    return [render(task_folder, doc, preset, cfg, backend, image_dir=image_dir)]


# ---- re-render CLI ------------------------------------------------------------

def rerender(task_folder: str, regenerate: set[int] = frozenset(), preset_name: str | None = None,
             final: bool = True, backend=None, image_dir: str | None = None) -> str:
    """Re-render combined-1.mp4 from an edited scenes.json, then optionally the final video."""
    doc = load_doc(task_folder)
    cfg = studio_config.get()
    preset = presets.load(preset_name or doc.get("preset") or cfg["default_preset"], cfg)
    unknown = set(regenerate) - {s["index"] for s in doc["scenes"]}
    if unknown:
        raise SceneError(f"no such scene indices: {sorted(unknown)}")
    style = preset.style or {}
    if style.get("seed_mode") == "random":
        for scene in doc["scenes"]:
            if scene["index"] in regenerate:
                scene["seed"] = random.SystemRandom().randrange(0, 2**31)
    if backend is None:
        aspect = "9:16" if doc["height"] >= doc["width"] else "16:9"
        backend = image_backends.create_backend(cfg, preset.style, aspect)
    combined = os.path.join(task_folder, "combined-1.mp4")
    render(task_folder, doc, preset, cfg, backend, set(regenerate), image_dir, output=combined)
    if final:
        rerender_final(task_folder)
    return combined


def rerender_final(task_folder: str, index: int = 1) -> str:
    """Re-run MPT's final render (and studio post-processing) for an existing task folder."""
    from app.models.schema import VideoParams
    from app.services import studio, video

    try:
        with open(os.path.join(task_folder, "script.json"), encoding="utf-8") as handle:
            raw = json.load(handle)["params"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise SceneError(f"script.json with task params is needed for the final render: {exc}") from exc
    params = VideoParams(**raw)
    final = os.path.join(task_folder, f"final-{index}.mp4")
    subtitle = os.path.join(task_folder, "subtitle.srt")
    generate = studio.render_override(params, video.generate_video)
    generate(
        video_path=os.path.join(task_folder, f"combined-{index}.mp4"),
        audio_path=os.path.join(task_folder, "audio.mp3"),
        subtitle_path=subtitle if os.path.isfile(subtitle) else "",
        output_file=final, params=params, bgm_file_override=None)
    studio.postprocess_final(os.path.basename(task_folder), params, index, "", "", "", final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.studio.scenes",
        description="Re-render a task from its (edited) scenes.json, then re-run the final render.")
    parser.add_argument("task_folder")
    parser.add_argument("--regenerate", default="",
                        help="comma-separated scene indices whose images are generated again")
    parser.add_argument("--preset")
    parser.add_argument("--no-final", action="store_true", help="only rebuild combined-1.mp4")
    args = parser.parse_args(argv)
    try:
        indices = {int(p) for p in args.regenerate.split(",") if p.strip()}
        print(rerender(args.task_folder, indices, args.preset, final=not args.no_final))
        return 0
    except (SceneError, presets.PresetError, ffmpeg_util.FFmpegError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

