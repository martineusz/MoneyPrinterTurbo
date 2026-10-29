"""Story mode: narration over one continuous background clip, with a title card and
word-level captions. Produces studio-combined-N.mp4 (one per part)."""

import json
import os
import re

from loguru import logger

from . import captions, ffmpeg_util, footage, presets, titlecard
from . import config as studio_config
from .srt import clip_cues, format_srt, read_srt

FPS = 30
CARD_FADE = 0.3
SAFETY = 0.1  # same margin MPT adds to its own combined videos
_SENTENCE = re.compile(r"(?<=[.!?…。！？])\s+")


class StoryError(RuntimeError):
    pass


def first_sentence(script: str, limit: int = 140) -> str:
    text = " ".join((script or "").split())
    sentence = _SENTENCE.split(text, maxsplit=1)[0] if text else ""
    if len(sentence) > limit:
        sentence = sentence[:limit - 1].rstrip() + "…"
    return sentence


def plan_parts(words: list[captions.Word], audio_duration: float,
               max_part_seconds: float) -> list[tuple[float, float]]:
    """Split the timeline into contiguous parts of at most ``max_part_seconds``.

    Cuts are placed after sentence ends; when a stretch has no sentence end the cut falls on
    the last word boundary that fits. The cut time is the middle of the pause between words.
    """
    if max_part_seconds <= 0 or audio_duration <= max_part_seconds or not words:
        return [(0.0, round(audio_duration, 3))]
    n, cuts, first, part_start = len(words), [], 0, 0.0
    while True:
        last = first  # last word that still fits in this part
        while last + 1 < n and words[last + 1].end - part_start <= max_part_seconds:
            last += 1
        if last + 1 >= n:
            break
        sentence_ends = [
            k for k in range(first, last + 1)
            if captions.SENTENCE_END.search(words[k].text)
            and words[k].end - part_start >= 0.5 * max_part_seconds
        ]
        k = sentence_ends[-1] if sentence_ends else last
        cut = round((words[k].end + words[k + 1].start) / 2, 3)
        cuts.append(cut)
        part_start, first = cut, k + 1
    bounds = [0.0, *cuts, round(audio_duration, 3)]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]


def _read_script(task_folder: str, params) -> str:
    try:
        with open(os.path.join(task_folder, "script.json"), encoding="utf-8") as handle:
            script = json.load(handle).get("script", "")
    except (OSError, json.JSONDecodeError):
        script = ""
    return script or params.video_script or ""


def _word_timeline(task_folder: str, script: str, audio_file: str, audio_duration: float,
                   cfg: dict, language: str | None, transcribe=None) -> list[captions.Word]:
    """Aligned word times, or line-level fallback when faster-whisper is unavailable."""
    srt_path = os.path.join(task_folder, "subtitle.srt")
    fallback = lambda: captions.lines_to_words(read_srt(srt_path)) if os.path.isfile(srt_path) else []  # noqa: E731
    transcribe = transcribe or (captions.transcribe if captions.whisper_available() else None)
    if transcribe is None:
        logger.warning(
            "studio: faster-whisper is not installed, so captions fall back to line-level "
            "timing. Install it for word-by-word captions: pip install faster-whisper")
        return fallback()
    try:
        heard = transcribe(audio_file, cfg["whisper_model"], cfg["whisper_device"], language)
    except Exception as exc:
        logger.warning(f"studio: transcription failed ({exc}); using line-level captions")
        return fallback()
    words = captions.align(captions.split_script_words(script), heard, audio_duration)
    return words or fallback()


def _render_part(footage_path: str, footage_start: float, length: float, width: int, height: int,
                 card_png: str, card_seconds: float, ass_path: str | None, fonts_dir: str | None,
                 out: str) -> None:
    from . import post  # reuse the subtitle filter helper

    card = min(card_seconds, length)
    fade_start = max(0.0, card - CARD_FADE)
    graph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={FPS},format=yuv420p[bg];"
        f"[1:v]format=rgba,fade=t=in:st=0:d=0.25:alpha=1,"
        f"fade=t=out:st={post._num(fade_start)}:d={CARD_FADE}:alpha=1[card];"
        f"[bg][card]overlay=0:0:enable='lt(t,{post._num(card)})':format=auto"
    )
    graph += f"[v0];[v0]{post._sub_filter(ass_path, fonts_dir)}[vout]" if ass_path else "[vout]"
    tmp = out + ".tmp.mp4"
    ffmpeg_util.run(ffmpeg_util.base_cmd() + [
        "-ss", f"{footage_start:.3f}", "-t", f"{length:.3f}", "-i", footage_path,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{card:.3f}", "-i", card_png,
        "-filter_complex", graph, "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{length:.3f}", tmp])
    os.replace(tmp, out)


def _extract_game_audio(footage_path: str, start: float, length: float, volume: float,
                        out: str) -> bool:
    try:
        ffmpeg_util.run(ffmpeg_util.base_cmd() + [
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", footage_path, "-vn",
            "-af", f"volume={volume}", "-c:a", "aac", out])
        return os.path.isfile(out)
    except ffmpeg_util.FFmpegError:
        logger.warning("studio: background footage has no usable audio track")
        return False


def build_for_task(task_id: str, params, audio_duration: float, transcribe=None,
                   rng=None) -> list[str]:
    from app.models.schema import VideoAspect
    from app.utils import utils

    task_folder = utils.task_dir(task_id)
    cfg = studio_config.get()
    preset = presets.load(params.studio_preset or cfg["default_preset"], cfg)
    story = (preset.style or {}).get("story", {})
    folder = params.studio_footage_dir or story.get("footage_dir") or cfg["footage_dir"]
    audio_file = os.path.join(task_folder, "audio.mp3")
    if not os.path.isfile(audio_file):
        raise StoryError(f"missing {audio_file}")
    width, height = VideoAspect(params.video_aspect).to_resolution()
    style = preset.style or {}
    width, height = int(style.get("width", width)), int(style.get("height", height))

    script = _read_script(task_folder, params)
    title = params.studio_story_title.strip() or first_sentence(script) or params.video_subject
    words = _word_timeline(task_folder, script, audio_file, audio_duration, cfg,
                           params.video_language or None, transcribe)
    if not words:
        logger.warning("studio: no caption timing available; the video will have no captions")

    split = params.studio_story_split_parts is True
    parts = plan_parts(words, audio_duration, cfg["max_part_seconds"]) if split else [
        (0.0, round(audio_duration, 3))]
    multi = len(parts) > 1
    if split and cfg["max_part_seconds"] <= 0:
        logger.warning("studio: split_parts is on but [studio] max_part_seconds is 0; not splitting")
    post_active = params.studio_post is True
    if multi:
        params.video_count = len(parts)  # one video per part
    if split and not post_active:
        raise StoryError("split_parts needs post-processing (enable studio_post) because each "
                         "part needs its own audio")

    cap_style = {**{"font": "Be Vietnam Pro"}, **story.get("captions", {})}
    fonts_dir = cap_style.get("fonts_dir") or (
        os.path.join(preset.root, "fonts") if os.path.isdir(os.path.join(preset.root, "fonts"))
        else utils.font_dir())
    all_cues = read_srt(os.path.join(task_folder, "subtitle.srt")) if os.path.isfile(
        os.path.join(task_folder, "subtitle.srt")) else []
    card_seconds = float(story.get("title_card", {}).get("seconds", cfg["title_card_seconds"]))
    volume = float(cfg["game_audio_volume"])
    outputs = []
    for n, (t0, t1) in enumerate(parts, 1):
        length = round(t1 - t0, 3)
        suffix = f"-part{n}" if multi else ""
        card_png = os.path.join(task_folder, f"title-card{suffix}.png")
        titlecard.render(card_png, width, height, title, story.get("title_card"),
                         label_extra=f"Part {n}" if multi else "")
        source, start = footage.pick_segment(folder, length + SAFETY, rng)
        logger.info(f"studio: story part {n}/{len(parts)} uses {os.path.basename(source)} @ {start}s")

        ass_path = None
        if words:
            in_part = [w for w in words if w.end > t0 and w.start < t1]
            ass_path = os.path.join(task_folder, f"captions{suffix}.ass")
            with open(ass_path, "w", encoding="utf-8") as handle:
                handle.write(captions.build_caption_ass(in_part, width, height, cap_style,
                                                        offset=t0, end_limit=length))
        if multi:
            part_audio = os.path.join(task_folder, f"audio-part{n}.mp3")
            ffmpeg_util.run(ffmpeg_util.base_cmd() + [
                "-ss", f"{t0:.3f}", "-t", f"{length:.3f}", "-i", audio_file, part_audio])
            with open(os.path.join(task_folder, f"subtitle-part{n}.srt"), "w", encoding="utf-8") as h:
                h.write(format_srt(clip_cues(all_cues, t0, t1)))
        if volume > 0:
            if post_active:
                _extract_game_audio(source, start, length, volume,
                                    os.path.join(task_folder, f"game-audio{suffix}.m4a"))
            else:
                logger.warning("studio: game_audio_volume needs post-processing "
                               "(studio_post) to be mixed in; ignoring it")
        out = os.path.join(task_folder, f"studio-combined-{n}.mp4")
        # post-processing owns caption burn-in when active (so under_grain etc. apply)
        _render_part(source, start, length + SAFETY, width, height, card_png, card_seconds,
                     None if post_active else ass_path, fonts_dir, out)
        outputs.append(out)
    return outputs
