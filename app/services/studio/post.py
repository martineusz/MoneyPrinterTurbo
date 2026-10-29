"""Post-processing: rebuild a final video from combined-N.mp4 + audio.mp3 + subtitle.srt.

Filter order (video), every step optional:
    lut3d -> eq -> gblur -> rgbashift -> [subtitles if under_grain] -> noise (grain)
    -> vignette -> cue visuals (glitch/invert/flash/black) -> [subtitles if not under_grain]
    -> format=yuv420p
Audio: narration (volume, voice_fx, silence cues) + BGM (looped, faded) + SFX (adelay)
    -> amix(normalize=0) -> alimiter.

CLI: python -m app.services.studio.post <task_folder> --preset NAME [--index N] [--dry-run]
"""

import argparse
import os
import random
import re
import shlex
import sys
from dataclasses import dataclass, field

from loguru import logger

from . import ass, ffmpeg_util, geometry, presets
from . import config as studio_config
from .srt import Cue, read_srt


@dataclass
class Event:
    time: float
    cue_index: int
    source: str  # "match: <line text>" or "at"
    cue: dict


@dataclass
class Plan:
    cmd: list[str]
    events: list[Event]
    output: str
    duration: float
    ass_path: str = ""
    ass_text: str = ""
    notes: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)


BAR = "@BAR@"  # marks where the tracking-bar overlay joins the video chain
LOOK_EFFECTS = ("scanlines", "chroma_bleed", "jitter", "tracking_bar", "fps", "frame_43", "timestamp")


def resolve_cues(cues: list[dict], lines: list[Cue]) -> list[Event]:
    """Turn cue rules into timed events (sorted by time)."""
    events: list[Event] = []
    for i, cue in enumerate(cues):
        if "at" in cue:
            events.append(Event(max(0.0, float(cue["at"])), i, "at", cue))
            continue
        pattern = re.compile(cue["match"], re.IGNORECASE)
        for line in lines:
            if pattern.search(line.text):
                base = line.end if cue.get("anchor", "start") == "end" else line.start
                events.append(Event(
                    max(0.0, base + float(cue.get("offset", 0))), i,
                    f"match: {line.text.replace(chr(10), ' ')[:60]}", cue))
                if cue.get("once"):
                    break
    return sorted(events, key=lambda e: (e.time, e.cue_index))


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _between(start: float, dur: float) -> str:
    return f"enable='between(t,{_num(start)},{_num(start + dur)})'"


def _sub_filter(ass_path: str, fonts_dir: str | None) -> str:
    out = f"ass=filename={ffmpeg_util.quote_filter_value(ass_path)}"
    if fonts_dir:
        out += f":fontsdir={ffmpeg_util.quote_filter_value(fonts_dir)}"
    return out


def timestamp_offset(ts: dict, seed_key: str) -> int:
    """Start of the on-screen counter in seconds. 'random' is stable per task folder + index."""
    start = ts.get("start", "00:00:00")
    if start == "random":
        return random.Random(seed_key).randrange(0, 86400)
    h, m, s = (int(p) for p in start.split(":"))
    return h * 3600 + m * 60 + s


def _timestamp_filter(ts: dict, offset: int, font: str, width: int, height: int,
                      box: tuple[int, int]) -> str:
    """drawtext camcorder counter. ``box`` is the visible (top, bottom) image area."""
    fmt = ts.get("format", "PLAY  %H:%M:%S").replace(":", "\\\\\\:")
    text = "%{pts\\:gmtime\\:" + str(offset) + "\\:" + fmt + "}"
    size = int(ts.get("size", 40))
    margin = int(width * 0.05)
    if ts.get("position", "top_left") == "bottom_right":
        x, y = f"w-text_w-{margin}", f"{box[1] - margin}-text_h"
    else:
        x, y = str(margin), str(box[0] + margin)
    color = ts.get("color", "#FFFFFF").lstrip("#")
    return (
        f"drawtext=fontfile={ffmpeg_util.quote_filter_value(font)}:text='{text}':x={x}:y={y}:"
        f"fontsize={size}:fontcolor=0x{color}:borderw={max(1, size // 18)}:bordercolor=black"
    )


def describe_effects(look: dict, ts_offset: int | None = None) -> list[str]:
    """Human-readable list of the analog effects that are on (for the dry-run output)."""
    out = []
    for key in LOOK_EFFECTS:
        value = look.get(key)
        if not value:
            continue
        if key == "timestamp":
            h, rem = divmod(ts_offset or 0, 3600)
            out.append(f"timestamp: {value.get('position', 'top_left')}, starts {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                       f", format {value.get('format', 'PLAY  %H:%M:%S')!r}")
        elif value is True:
            out.append(key)
        else:
            out.append(f"{key}: {value}")
    return out


def _band_source(width: int, height: int) -> str:
    """Noisy horizontal band for the tracking bar (a second, lavfi, input of the same pass)."""
    bh = max(16, int(height * 0.07))
    return (f"nullsrc=s={width}x{bh}:r=30,format=yuva444p,"
            f"geq=lum='random(1)*255':cb=128:cr=128:a='90*sin(PI*Y/H)'")


def _video_graph(steps: list[str], band_input: int | None) -> str:
    if BAR not in steps:
        return f"[0:v]{','.join(steps)}[vout]"
    i = steps.index(BAR)
    pre = ",".join(steps[:i]) or "null"
    post = ",".join(steps[i + 1:])
    return (
        f"[0:v]{pre}[base];[{band_input}:v]gblur=sigma=1:sigmaV=0.5[band];"
        f"[base][band]overlay=x=0:y='(mod(t,10)/4)*(H+h)-h':format=auto:"
        f"enable='lt(mod(t,10),4)',{post}[vout]"
    )


def _video_chain(post: dict, events: list[Event], ass_path: str, fonts_dir: str | None,
                 pad: float, size: tuple[int, int] = (1080, 1920), ts_offset: int = 0,
                 ts_font: str = "") -> list[str]:
    look = post.get("look", {})
    sub = post.get("subtitles", {})
    width, height = size
    burn = bool(ass_path)
    under = bool(sub.get("under_grain", False))
    steps: list[str] = []
    if pad > 0:
        steps.append(f"tpad=stop_mode=clone:stop_duration={_num(pad)}")
    if look.get("lut"):
        steps.append(f"lut3d=file={ffmpeg_util.quote_filter_value(look['lut'])}")
    eq = {k: look[k] for k in ("contrast", "brightness", "saturation", "gamma") if k in look}
    if eq:
        steps.append("eq=" + ":".join(f"{k}={_num(v)}" for k, v in eq.items()))
    if look.get("blur"):
        steps.append(f"gblur=sigma={_num(look['blur'])}")
    if look.get("chroma_offset"):
        n = int(look["chroma_offset"])
        steps.append(f"rgbashift=rh={-n}:bh={n}")
    box = (0, height)
    if look.get("frame_43") and (frame := geometry.frame43_box(width, height)):
        top, crop_h = frame
        steps.append(f"crop={width}:{crop_h}:0:{top},pad={width}:{height}:0:{top}:black")
        box = (top, top + crop_h)
    if look.get("jitter"):
        m = max(2, round(float(look["jitter"]) * width * 0.02))
        x = (f"{m}+{m}*(0.5*sin(37*t)+0.3*sin(11*t))"
             f"+{2 * m}*gt(sin(2.3*t)*sin(0.7*t),0.9)*sin(90*t)")
        steps.append(f"crop=w=iw-{2 * m}:h=ih:x='{x}':y=0,scale={width}:{height},setsar=1")
    if look.get("chroma_bleed"):
        steps.append(f"format=yuv420p,gblur=sigma={_num(look['chroma_bleed'])}:sigmaV=0:planes=6")
    if look.get("tracking_bar"):
        steps.append(BAR)
    if look.get("scanlines"):
        period = 2 * max(1, round(height / 960))
        steps.append(
            f"drawgrid=w=0:h={period}:t={period // 2}:c=black@{_num(look['scanlines'])}")
    if look.get("timestamp"):
        steps.append(_timestamp_filter(look["timestamp"], ts_offset, ts_font, width, height, box))
    if burn and under:
        steps.append(_sub_filter(ass_path, fonts_dir))
    if look.get("grain"):
        steps.append(f"noise=alls={_num(look['grain'])}:allf=t+u")
    if look.get("vignette"):
        steps.append(f"vignette=angle={_num(look['vignette'] * 1.5708)}:eval=init")
    for ev in events:
        c = ev.cue
        if c.get("glitch"):
            window = _between(ev.time, c["glitch"])
            steps.append(f"rgbashift=rh=-14:bh=14:gv=6:rv=5:bv=-5:{window}")
            steps.append(f"noise=alls=45:allf=t+u:{window}")
        if c.get("invert"):
            steps.append(f"negate={_between(ev.time, c['invert'])}")
        if c.get("flash"):
            steps.append(f"eq=brightness=0.6:{_between(ev.time, c['flash'])}")
        if c.get("black"):
            steps.append(
                f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:{_between(ev.time, c['black'])}")
    if burn and not under:
        steps.append(_sub_filter(ass_path, fonts_dir))
    if look.get("fps"):
        steps.append(f"fps={int(look['fps'])}")
    steps.append("format=yuv420p")
    return steps


_AFMT = "aformat=sample_rates=48000:channel_layouts=stereo"


def _audio_graph(post: dict, events: list[Event], duration: float, n_inputs_before_sfx: int,
                 has_bgm: bool, game_input: int | None = None
                 ) -> tuple[list[str], list[tuple[int, Event]]]:
    """Return audio filtergraph parts; sfx inputs are numbered after the BGM input."""
    parts: list[str] = []
    narr = [_AFMT, f"volume={_num(post.get('voice_volume', 1.0))}"]
    if post.get("voice_fx"):
        narr.append(post["voice_fx"])
    for ev in events:
        n = ev.cue.get("silence_before")
        if n:
            narr.append(f"volume=0:{_between(max(0.0, ev.time - n), min(n, ev.time))}")
    parts.append(f"[1:a]{','.join(narr)}[narr]")
    mix = ["[narr]"]
    if has_bgm:
        bgm = post["bgm"]
        chain = [_AFMT, f"atrim=duration={_num(duration)}", f"volume={_num(bgm.get('volume', 0.2))}"]
        fade = bgm.get("fade_out", 0)
        if fade:
            chain.append(f"afade=t=out:st={_num(max(0.0, duration - fade))}:d={_num(fade)}")
        parts.append(f"[2:a]{','.join(chain)}[bgm]")
        mix.append("[bgm]")
    if game_input is not None:  # story mode background-footage audio (already volume-scaled)
        parts.append(f"[{game_input}:a]{_AFMT},atrim=duration={_num(duration)}[game]")
        mix.append("[game]")
    sfx: list[tuple[int, Event]] = []
    next_input = n_inputs_before_sfx
    for ev in events:
        if ev.cue.get("sfx"):
            ms = int(round(ev.time * 1000))
            vol = ev.cue.get("volume", 1.0)
            parts.append(
                f"[{next_input}:a]{_AFMT},volume={_num(vol)},adelay={ms}|{ms}[sfx{next_input}]")
            mix.append(f"[sfx{next_input}]")
            sfx.append((next_input, ev))
            next_input += 1
    parts.append(
        f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95[aout]")
    return parts, sfx


def _part_file(folder: str, name: str, index: int, ext: str) -> str:
    """Story split-parts write <name>-part<N><ext>; prefer it over the whole-task file."""
    part = os.path.join(folder, f"{name}-part{index}{ext}")
    return part if os.path.isfile(part) else os.path.join(folder, f"{name}{ext}")


def build_plan(task_folder: str, preset: presets.Preset, index: int = 1,
               output: str | None = None, write_files: bool = True) -> Plan:
    post = preset.post
    if post is None:
        raise presets.PresetError([f"[{preset.name}] has no post.json"])
    cfg = studio_config.get()
    ffmpeg = ffmpeg_util.get_ffmpeg()
    video_in = os.path.join(task_folder, f"combined-{index}.mp4")
    audio_in = _part_file(task_folder, "audio", index, ".mp3")
    srt_in = _part_file(task_folder, "subtitle", index, ".srt")
    story_ass = _part_file(task_folder, "captions", index, ".ass")
    game_audio = _part_file(task_folder, "game-audio", index, ".m4a")
    for needed in (video_in, audio_in):
        if not os.path.isfile(needed):
            raise FileNotFoundError(f"missing {needed}")

    vinfo = ffmpeg_util.probe(video_in, ffmpeg)
    duration = ffmpeg_util.probe(audio_in, ffmpeg).duration
    pad = max(0.0, duration - vinfo.duration)
    notes: list[str] = []
    if pad > 0.25:
        notes.append(f"video is {pad:.2f}s shorter than narration; last frame will be held")

    lines: list[Cue] = []
    if os.path.isfile(srt_in):
        lines = read_srt(srt_in)
    events = resolve_cues(post.get("cues", []), lines)
    if post.get("cues") and not lines and any("match" in c for c in post["cues"]):
        notes.append("subtitle.srt is missing or empty: 'match' cues cannot fire")

    sub = post.get("subtitles", {})
    ass_path, ass_text = "", ""
    wants_subs = sub.get("enabled", True) is not False
    if os.path.isfile(story_ass):
        ass_path = story_ass  # story mode owns captions; post.json subtitles are ignored
        if sub:
            notes.append("story captions in use; post.json 'subtitles' ignored")
    elif wants_subs and lines:
        ass_path = os.path.join(task_folder, f"post-{index}.ass")
        ass_text = ass.build_ass(lines, vinfo.width, vinfo.height, sub)
    elif wants_subs:
        notes.append("no subtitle.srt: subtitles skipped")
    if ass_path and not ffmpeg_util.has_libass(ffmpeg):
        ffmpeg_util.require_libass(ffmpeg)
        raise ffmpeg_util.FFmpegError("ffmpeg has no libass; cannot burn subtitles")

    fonts_dir = sub.get("fonts_dir")
    if not fonts_dir and os.path.isdir(os.path.join(preset.root, "fonts")):
        fonts_dir = os.path.join(preset.root, "fonts")

    has_bgm = bool(post.get("bgm", {}).get("file"))
    out_path = output or os.path.join(task_folder, f"final-{index}.mp4")
    cmd = ffmpeg_util.base_cmd(ffmpeg) + ["-i", video_in, "-i", audio_in]
    if has_bgm:
        cmd += ["-stream_loop", "-1", "-i", post["bgm"]["file"]]
    n_inputs = 3 if has_bgm else 2
    game_input = None
    if os.path.isfile(game_audio):
        cmd += ["-i", game_audio]
        game_input, n_inputs = n_inputs, n_inputs + 1
    audio_parts, sfx = _audio_graph(post, events, duration, n_inputs, has_bgm, game_input)
    for _, ev in sfx:
        cmd += ["-i", ev.cue["sfx"]]
    look = post.get("look", {})
    ts_offset, ts_font = 0, ""
    if look.get("timestamp"):
        ts_offset = timestamp_offset(look["timestamp"], f"{os.path.basename(task_folder)}-{index}")
        ts_font = look["timestamp"].get("font") or _default_font(preset.root)
    steps = _video_chain(post, events, ass_path, fonts_dir, pad, (vinfo.width, vinfo.height),
                         ts_offset, ts_font)
    band_input = None
    if BAR in steps:
        band_input = n_inputs + len(sfx)
        cmd += ["-f", "lavfi", "-i", _band_source(vinfo.width, vinfo.height)]
    graph = ";".join([_video_graph(steps, band_input)] + audio_parts)
    x264 = post.get("x264", {})
    tmp_out = out_path + ".tmp.mp4"
    cmd += [
        "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", x264.get("preset", cfg["x264_preset"]),
        "-crf", str(x264.get("crf", cfg["x264_crf"])), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-t", _num(duration), tmp_out,
    ]
    plan = Plan(cmd=cmd, events=events, output=out_path, duration=duration,
                ass_path=ass_path if ass_text else "", ass_text=ass_text, notes=notes,
                effects=describe_effects(look, ts_offset))
    if write_files and ass_text:
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write(ass_text)
    return plan


def _default_font(preset_root: str) -> str:
    """First font in the preset's fonts/ folder, else the bundled Be Vietnam Pro."""
    folder = os.path.join(preset_root, "fonts")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".ttf", ".otf")):
                return os.path.join(folder, name)
    from app.utils import utils

    return os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")


def format_events(plan: Plan) -> str:
    rows = [f"{e.time:8.2f}s  cue#{e.cue_index}  {e.source}" for e in plan.events] or ["(no cue events)"]
    if plan.effects:
        rows += ["", "look effects:"] + [f"  {line}" for line in plan.effects]
    return "\n".join(rows)


def dry_run_plan(task_folder: str, preset_name: str, index: int = 1) -> Plan:
    """Resolve cues/effects and build the ffmpeg command without running or writing anything."""
    return build_plan(task_folder, presets.load(preset_name), index, write_files=False)


def dry_run(task_folder: str, preset_name: str, index: int = 1) -> tuple[list[str], list[Event]]:
    plan = dry_run_plan(task_folder, preset_name, index)
    return plan.cmd, plan.events


def run_plan(plan: Plan) -> str:
    tmp_out = plan.cmd[-1]
    try:
        ffmpeg_util.run(plan.cmd)
        os.replace(tmp_out, plan.output)
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    return plan.output


def apply_to_final(task_folder: str, preset: presets.Preset, index: int = 1) -> str:
    """Post-process into final-N.mp4, preserving the original as final-N.raw.mp4."""
    final = os.path.join(task_folder, f"final-{index}.mp4")
    raw = os.path.join(task_folder, f"final-{index}.raw.mp4")
    plan = build_plan(task_folder, preset, index, output=final)
    for note in plan.notes:
        logger.warning(f"studio post: {note}")
    if os.path.isfile(final) and not os.path.isfile(raw):
        os.replace(final, raw)
    try:
        return run_plan(plan)
    except Exception:
        if not os.path.isfile(final) and os.path.isfile(raw):
            os.replace(raw, final)  # never leave the task without a final video
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.studio.post",
        description="Rebuild final-N.mp4 from a task folder using a studio post.json preset.")
    parser.add_argument("task_folder")
    parser.add_argument("--preset", help="preset name (default: [studio] default_preset)")
    parser.add_argument("--index", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true",
                        help="list resolved cue events and print the ffmpeg command")
    args = parser.parse_args(argv)
    try:
        name = args.preset or studio_config.get()["default_preset"]
        if args.dry_run:
            plan = dry_run_plan(args.task_folder, name, args.index)
            print(format_events(plan))
            print()
            print(" ".join(shlex.quote(c) for c in plan.cmd))
            return 0
        preset = presets.load(name)
        print(apply_to_final(args.task_folder, preset, args.index))
        return 0
    except (presets.PresetError, FileNotFoundError, ffmpeg_util.FFmpegError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
