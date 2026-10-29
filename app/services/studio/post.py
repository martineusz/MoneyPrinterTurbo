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
import re
import shlex
import sys
from dataclasses import dataclass, field

from loguru import logger

from . import ass, ffmpeg_util, presets
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


def _video_chain(post: dict, events: list[Event], ass_path: str, fonts_dir: str | None,
                 pad: float) -> str:
    look = post.get("look", {})
    sub = post.get("subtitles", {})
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
    steps.append("format=yuv420p")
    return ",".join(steps)


_AFMT = "aformat=sample_rates=48000:channel_layouts=stereo"


def _audio_graph(post: dict, events: list[Event], duration: float, n_inputs_before_sfx: int,
                 has_bgm: bool) -> tuple[list[str], list[tuple[int, Event]]]:
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


def build_plan(task_folder: str, preset: presets.Preset, index: int = 1,
               output: str | None = None, write_files: bool = True) -> Plan:
    post = preset.post
    if post is None:
        raise presets.PresetError([f"[{preset.name}] has no post.json"])
    cfg = studio_config.get()
    ffmpeg = ffmpeg_util.get_ffmpeg()
    video_in = os.path.join(task_folder, f"combined-{index}.mp4")
    audio_in = os.path.join(task_folder, "audio.mp3")
    srt_in = os.path.join(task_folder, "subtitle.srt")
    story_ass = os.path.join(task_folder, "captions.ass")
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
    audio_parts, sfx = _audio_graph(post, events, duration, n_inputs, has_bgm)
    for _, ev in sfx:
        cmd += ["-i", ev.cue["sfx"]]
    graph = ";".join([f"[0:v]{_video_chain(post, events, ass_path, fonts_dir, pad)}[vout]"] + audio_parts)
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
                ass_path=ass_path if ass_text else "", ass_text=ass_text, notes=notes)
    if write_files and ass_text:
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write(ass_text)
    return plan


def format_events(plan: Plan) -> str:
    rows = [f"{e.time:8.2f}s  cue#{e.cue_index}  {e.source}" for e in plan.events] or ["(no cue events)"]
    return "\n".join(rows)


def dry_run(task_folder: str, preset_name: str, index: int = 1) -> tuple[list[str], list[Event]]:
    """Resolve cues and build the ffmpeg command without running or writing anything."""
    preset = presets.load(preset_name)
    plan = build_plan(task_folder, preset, index, write_files=False)
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
            cmd, events = dry_run(args.task_folder, name, args.index)
            print(format_events(Plan(cmd, events, "", 0)))
            print()
            print(" ".join(shlex.quote(c) for c in cmd))
            return 0
        preset = presets.load(name)
        print(apply_to_final(args.task_folder, preset, args.index))
        return 0
    except (presets.PresetError, FileNotFoundError, ffmpeg_util.FFmpegError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
