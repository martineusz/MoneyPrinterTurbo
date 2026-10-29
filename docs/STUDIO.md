# Studio

Studio adds three features for short vertical videos on top of MoneyPrinterTurbo (MPT). All new code lives in `app/services/studio/` (plus `webui/studio_panel.py`, `presets/`, tests in `test/services/test_studio_*.py`). Upstream files only carry small hooks wrapped in `# --- studio hook ---` / `# --- end studio hook ---`. With every studio option off MPT behaves exactly like upstream.

1. **Post-processing** — rebuilds `final-N.mp4` from `combined-N.mp4` + `audio.mp3` + `subtitle.srt` in one ffmpeg pass: look (LUT, grain, …), styled subtitles, BGM, SFX cues, glitches.
2. **Scene mode** (`video_source = "studio_scenes"`) — one generated image per narration segment, in order, with camera motion.
3. **Story mode** (`video_source = "studio_story"`) — narration over one continuous background clip, a title card and word-by-word captions.

## Using it

| Where | How |
|---|---|
| WebUI | Open the **Studio** expander: pick a preset, tick *Post-process*, choose *Studio mode* (scene/story), set story title / footage folder / split parts. The *Scenes* editor lists tasks with a `scenes.json`; edit prompt/seed/motion, save, or *Regenerate selected scenes*. |
| CLI | `python cli.py --video-subject ... --video-source studio_scenes --studio-preset NAME --studio-post` (also `--studio-story-title`, `--studio-footage-dir`, `--studio-split-parts`). |
| API | `studio_post`, `studio_preset`, `studio_story_title`, `studio_footage_dir`, `studio_story_split_parts` on the task request; `video_source` may be `studio_scenes` / `studio_story`. |
| Post only | `python -m app.services.studio.post <task_folder> --preset NAME [--index N] [--dry-run]` |
| Scenes only | `python -m app.services.studio.scenes <task_folder> [--regenerate 2,5] [--no-final]` |

Pipeline order is unchanged: script → terms → audio → subtitle → materials → rendering. Scene mode needs subtitles enabled (it segments `subtitle.srt`). Studio sources still trigger MPT's term generation (unused, cheap).

## Architecture

```
app/services/studio/
  __init__.py       hook API: VIDEO_SOURCES, preflight, prepare_materials, combine_override,
                    render_override, postprocess_final
  config.py         [studio] section (read from config._cfg; config.py upstream untouched)
  presets.py        preset discovery, strict validation of style.json / post.json
  ffmpeg_util.py    binary (video.get_ffmpeg_binary), libass check, filter value quoting, probe, run
  srt.py ass.py     SRT parse/format/clip; ASS styles, escaping, colors
  post.py           Phase 1 filter graph, cue resolution, dry run, CLI
  segmentation.py   scenes that tile the audio
  scene_prompts.py  LLM shot descriptions (JSON, validated, one retry), entity substitution
  image_backends.py openai (reuses material.py helpers) / ComfyUI, hash cache
  motion.py scenes.py  zoompan motions, scenes.json, rendering, re-render CLI
  footage.py        continuous segment picker + footage_usage.json log
  titlecard.py      Pillow title card (original design, no platform imitation)
  captions.py       faster-whisper (lazy), alignment to script, chunking, ASS word highlight
  story.py          story orchestration, split parts
  materials.py      glue for the materials/combine hooks
  cli_args.py       CLI options (import-light)
webui/studio_panel.py
```

Data flow for the new sources: `get_video_materials` hook → `studio.prepare_materials` builds the pre-rendered visuals (`studio-combined[-N].mp4`) → `combine_override` replaces `video.combine_videos` with a copy to `combined-N.mp4` (upstream's combine would chop and loop an assembled video) → MPT's render → `postprocess_final`.

Behaviour notes:
- **Post-processing owns subtitles and BGM.** MPT's own burn-in/BGM still run (the raw result is kept as `final-N.raw.mp4`) unless `skip_mpt_render = true`, which replaces MPT's MoviePy render with a plain mux. A failed post-process keeps MPT's output and logs the error.
- **Story captions**: with post-processing on, story mode writes `captions.ass` and `post.py` burns it (so `under_grain` applies; `post.json` `subtitles` are ignored). With it off, captions are burned into the combined clip and MPT's own subtitle burn-in is suppressed (no duplicates).
- **Game audio** (`game_audio_volume > 0`) is mixed only when post-processing is on.
- **Split parts** (`studio_story_split_parts` + `max_part_seconds`) needs post-processing: each part gets `audio-partN.mp3`, `subtitle-partN.srt`, `captions-partN.ass`; `video_count` is set to the number of parts; MPT's render is replaced by a mux using the part audio. Cuts fall after sentence ends; a trailing remainder can be short.
- **libass**: required for any subtitle/caption burn-in. If the ffmpeg build lacks the `ass`/`subtitles` filters, a clear error naming the binary is logged and the task fails in preflight.
- faster-whisper is optional and imported lazily; without it story captions fall back to line-level timing with a log hint.

## Post-processing filter order

Video: `lut3d → eq → gblur → rgbashift → [subtitles if under_grain] → noise (temporal grain) → vignette → cue visuals (glitch / invert / flash / black via enable=between(t,a,b)) → [subtitles if not under_grain] → format=yuv420p`.
Audio: narration (`voice_volume`, `voice_fx`, silence cues) + BGM (looped, trimmed, fade-out) + game audio + SFX (`adelay`) → `amix(normalize=0)` → `alimiter`. Output H.264 + AAC, `+faststart`, duration = narration (a short video's last frame is held).

## Configuration

### `[studio]` in config.toml (all optional)

| Key | Default | Meaning |
|---|---|---|
| `presets_dir` | `"presets"` | Folder of presets (relative to project root) |
| `default_preset` | `"example"` | Preset used when a task names none |
| `skip_mpt_render` | `false` | With post on, replace MPT's MoviePy render by a mux |
| `x264_preset` / `x264_crf` | `"veryfast"` / `18` | Defaults for post output |
| `image_backend` | `"openai"` | `"openai"` (reuses `[app] openai_image_*`) or `"comfyui"` |
| `comfyui_url` / `comfyui_timeout` | `http://127.0.0.1:8188` / `300` | ComfyUI |
| `min_scene_seconds` / `max_scene_seconds` | `2.0` / `5.0` | Merge/split thresholds |
| `scene_motions` | all seven | `zoom_in zoom_out pan_left pan_right pan_up pan_down static` |
| `scene_motion_order` | `"cycle"` | `"cycle"` or `"random"` (seeded by scene seed) |
| `footage_dir` | `""` | Story footage folder (task option / preset `story.footage_dir` override) |
| `game_audio_volume` | `0.0` | Background footage audio (0 = muted) |
| `title_card_seconds` | `3.0` | Title card duration |
| `whisper_model` / `whisper_device` | `"small"` / `"auto"` | faster-whisper (auto = CUDA when available) |
| `max_part_seconds` | `0` | >0 enables splitting with `studio_story_split_parts` |

### Preset folder

```
presets/<name>/
  style.json   post.json
  sounds/  luts/  fonts/        (optional; relative paths resolve against the preset folder)
```
Every file is validated on load: unknown keys (with "did you mean"), wrong types, bad colors/regex, missing referenced files; all problems are reported together.

**post.json**: `look{lut, contrast, brightness, saturation, gamma, blur, chroma_offset, vignette, grain, + analog effects below}`, `subtitles{enabled, font, fonts_dir, size, color, outline_color, outline_width, shadow, uppercase, position top|middle|bottom, margin_v, letter_spacing, under_grain}`, `voice_volume`, `voice_fx` (ffmpeg audio filter string), `bgm{file, volume, fade_out}`, `x264{preset, crf}`, and `cues[]`:
`match` (case-insensitive regex per subtitle line) or `at` (seconds), plus `once`, `anchor start|end`, `offset`, and any of `sfx`+`volume`, `glitch` (seconds, `glitch_style: "shift"`), `invert`/`flash`/`black` (seconds), `silence_before` (seconds).

**style.json**: `style_prefix` (prepended to every scene prompt, before the LLM's description), `style_suffix` (appended), `prompt_guidance` (free text added to the instructions that turn narration into visual prompts, e.g. camera or composition rules), `negative_prompt`, `entities` (name → fixed description, substituted wherever the LLM names them), `seed_mode fixed|per_scene|random`, `base_seed`, `width`, `height` (size requested from the image model; the video is always rendered at the aspect resolution, e.g. 1080x1920, scaling the image), `comfyui_workflow` (JSON with `{prompt} {negative} {seed} {width} {height}` placeholders), and `story{footage_dir, title_card{background_color, opacity, radius, channel_name, avatar, font, title_size, title_color, channel_size, channel_color, label, label_color, label_background}, captions{font, fonts_dir, size, color, highlight_color, outline_color, outline_width, shadow, uppercase, position, margin_v, letter_spacing, max_words, max_chars}}`.

### Analog / VHS look effects (post.json `look`, all optional and off by default)

| Key | Value | Effect |
|---|---|---|
| `frame_43` | `true` | Image rendered as a centered 4:3 picture with black bars inside the 9:16 frame (center crop) |
| `jitter` | 0-1 | Horizontal tracking wobble: small time-varying shift, occasionally stronger (crop + rescale) |
| `chroma_bleed` | pixels (0-64) | Horizontal-only blur of the chroma planes (colour smearing) |
| `tracking_bar` | `true` | Soft noisy band that rolls down the frame for ~4 s every 10 s (a second lavfi input of the same pass, overlaid) |
| `scanlines` | 0-1 | Darkens every other line (period scales with resolution) with that opacity |
| `timestamp` | `{format, start, position, font, size, color}` | Camcorder counter drawn with drawtext. `format` is strftime-like (`"PLAY  %H:%M:%S"`, no `'` or backslash); `start` is `"HH:MM:SS"` or `"random"` (stable per task folder + video index, so dry-run and render agree); `position` is `top_left` or `bottom_right` (inside the 4:3 picture when `frame_43` is on); `font` is a font file in the preset (default: first font in `fonts/`, else Be Vietnam Pro) |
| `fps` | 1-60 | Output frame rate drop (e.g. `15`) for a choppy tape feel |

**`frame_43` and scene mode.** When post-processing is on and `look.frame_43` is set, scene mode renders every clip so the image (and its zoom/pan) fills exactly the centered 4:3 box that post keeps; the rest of the frame is black. Both sides get the box from one helper (`geometry.frame43_box`), so the crop discards nothing. Generate 4:3 images for this: set `style.json` `width`/`height` to e.g. `1152x864` or `1024x768` (loading the preset logs a warning when they are not close to 4:3 landscape; a portrait image would be center-cropped into the box). `scenes.json` stores `"frame_43": true` so re-renders keep the geometry. **Story mode is unchanged:** the background footage is still center-cropped to the full 9:16 frame and then to the 4:3 box by post, so with `frame_43` only the middle 4:3 band of the footage stays visible.

Order inside the single pass: `... rgbashift -> frame_43 -> jitter -> chroma_bleed -> tracking_bar -> scanlines -> timestamp -> [subtitles if under_grain] -> grain -> vignette -> cues -> [subtitles if not under_grain] -> fps -> yuv420p`. So scanlines, counter and under-grain subtitles degrade with the grain, and fps drops frames last. `--dry-run` prints a "look effects:" list (including the resolved counter start) before the ffmpeg command.

### Creating a preset
Copy `presets/example/`, rename it, edit the JSON (the example holds neutral placeholders only), drop files into `sounds/ luts/ fonts/`, reference them by relative path, then check with `python -m app.services.studio.post <task_folder> --preset NAME --dry-run`.

### scenes.json (task folder)
`{version, preset, audio_duration, width, height, scenes:[{index, start, end, narration, prompt, seed, image, image_key, motion}]}`. `prompt` excludes the style suffix (added at render time). Scenes must tile `0..audio_duration` (checked on load). Images are cached in `storage/studio_cache/` by `sha256(prompt+suffix, negative, seed, size, backend)`, so only edited scenes regenerate; clips are cached per image/motion/length in `<task>/scene_clips/`.

## Rebasing on upstream

Hook points, in order of conflict likelihood. After a rebase run `git grep -n "studio hook"` — you should find exactly these.

| File | Where | What |
|---|---|---|
| `app/services/task.py` | imports, after `from app.services import upload_post` | `from app.services import studio` |
| `app/services/task.py` | after `_SUPPORTED_VIDEO_SOURCES = frozenset({...})` | union with `studio.VIDEO_SOURCES` |
| `app/services/task.py` | `_run_pipeline`, just before `# 1. Generate script` | `studio.preflight(...)` → `_mark_task_failed` |
| `app/services/task.py` | `get_video_materials`, top | studio sources return `studio.prepare_materials(...)` |
| `app/services/task.py` | `generate_final_videos`, before the `combine_videos(` call | `_combine_videos = studio.combine_override(...)`; **call renamed** `video.combine_videos(` → `_combine_videos(` |
| `app/services/task.py` | `generate_final_videos`, around `video.generate_video(` | `_generate_video = studio.render_override(...)`; **call renamed** `video.generate_video(` → `_generate_video(`; then `studio.postprocess_final(...)` right after the call |
| `app/models/schema.py` | end of `VideoParams` | five `studio_*` fields |
| `cli.py` | after `_CLI_VIDEO_SOURCES`, before `parser.parse_args(argv)`, before `optional_arg_names` | extend sources; add/read studio args |
| `webui/Main.py` | import block after `from app.utils import utils`; source validation (`Please Select a Valid Video Source`); after `_render_subtitle_settings(right_panel, params)` | `studio_panel` / `studio_service` imports; `] + sorted(studio_service.VIDEO_SOURCES):` on the validation list; `studio_panel.render(params, tr)` |
| `config.example.toml` | end of file | documented `[studio]` section |

If upstream renames `combine_videos` / `generate_video` / `get_video_materials`, re-point the corresponding hook; the studio functions take the same keyword arguments (`combine_override` returns a callable accepting `combined_video_path, video_paths, audio_file, **kwargs`; `render_override` returns one accepting `video_path, audio_path, subtitle_path, output_file, params, bgm_file_override`). Studio calls these private upstream helpers: `material._openai_image_endpoint/_openai_image_size/_request_openai_image/_save_openai_image_file/is_openai_image_enabled`, `llm._generate_response/_strip_code_fence/_extract_embedded_json`, `video.get_ffmpeg_binary/subtitle_font_supports_text/generate_video`, `utils.task_dir/font_dir/storage_dir/root_dir`.

## Tests

`uv run python -X utf8 -m pytest -q test/services/test_studio_*.py` covers SRT/ASS/escaping, preset validation, cue resolution, post end-to-end (lavfi media, paths with spaces/apostrophes), segmentation tiling, prompt validation and retry, entity substitution, ComfyUI client, scene rendering with a mocked image backend, footage picker, alignment, chunking, title card, story end-to-end (with and without post, split parts), CLI and WebUI hooks.
