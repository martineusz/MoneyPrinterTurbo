# Studio (branch `studio`) — plan

Status: **Phase 0 plan, awaiting approval. No code has been written.**

Studio adds three features on top of MoneyPrinterTurbo (MPT) for short vertical videos:

1. **Post-processing** — rebuilds the final video from a task's `combined-N.mp4` + `audio.mp3` + `subtitle.srt` with a look (LUT, grain, …), styled subtitles, BGM, SFX cues and glitches, in one ffmpeg pass.
2. **Scene mode** (`video_source = "studio_scenes"`) — one generated image per narration segment, rendered in order with motion.
3. **Story mode** (`video_source = "studio_story"`) — narration over one continuous background clip, title card, word-level captions.

Core rule: the diff against upstream stays tiny. All new code lives in `app/services/studio/`; upstream files only get marked hooks (`# --- studio hook ---` … `# --- end studio hook ---`). With every studio option off, behaviour is identical to upstream.

---

## 1. What I found in the code (facts the design relies on)

- Pipeline order in `task._run_pipeline` (task.py:1549): preflight → script → terms → audio → subtitle → materials → `generate_final_videos`. `subtitle.srt` therefore exists before materials, as you said.
- `generate_final_videos` (task.py:1010) loops `video_count` times: `video.combine_videos(...)` writes `combined-N.mp4`, then `video.generate_video(...)` (MoviePy) burns subtitles, mixes BGM and writes `final-N.mp4`.
- `combine_videos` (video.py:973) cuts every material into `video_clip_duration` segments and loops/shuffles them until the audio length is reached. **Feeding it an already-assembled video would chop and loop it** (in sequential mode only the first segment is kept). So scene/story mode cannot just return their video as a "material"; they must replace the combine step. This is the one non-trivial hook.
- `_SUPPORTED_VIDEO_SOURCES` (task.py:102) is the only server-side source whitelist. The CLI has its own `_CLI_VIDEO_SOURCES` (cli.py:40, also used for batch validation) and the WebUI has `VIDEO_SOURCE_GROUPS` (Main.py:136) plus a labels dict (Main.py:6029).
- openai_image: `material._openai_image_endpoint()` / `_openai_image_size()` / `_request_openai_image(endpoint, payload) -> (bytes|None, error)` / `_save_openai_image_file(bytes, dir)` (material.py:1345–1700). They are private names, but they are the helpers to reuse (no copying). Config keys are `openai_image_base_url`, `openai_image_api_keys`, `openai_image_model`, `openai_image_size` in `[app]`.
- Config: `config._cfg` is the whole parsed TOML and `save_config()` writes `dict(_cfg)` back, so an unknown `[studio]` section **survives a WebUI save**. Studio can read it without touching `app/config/config.py`.
- ffmpeg: `utils.get_ffmpeg_binary()` (via `video.get_ffmpeg_binary()`); here it resolves to the bundled imageio-ffmpeg 7.1, which has libass, `lut3d`, `rgbashift`, `noise`, `vignette`, `amix`, `alimiter`. Studio will check for libass at runtime anyway.
- i18n: `webui/i18n/*.json` has az ca de en es fr id it ko pt ru tr vi zh. **There is no Polish file.** `test_webui_i18n.py` enforces key parity across secondary locales, so adding keys to `en.json` would force edits to 12 other files plus that test (see Open questions).
- Not installed in the venv right now: `faster-whisper`. Pillow comes in through MoviePy.

---

## 2. Hook points in upstream files

All hooks are additive. Line numbers are approximate (current `feat/studio` HEAD).

| # | File / function | ~Line | Hook | Upstream lines changed |
|---|---|---|---|---|
| H1 | `app/models/schema.py` `VideoParams` | after 169 (end of class) | New fields with upstream-neutral defaults: `studio_preset: str = ""`, `studio_post: bool = False`, `studio_story_title: str = ""`, `studio_footage_dir: str = ""`, `studio_story_split_parts: bool = False` | +6 |
| H2 | `app/services/task.py` imports + `_SUPPORTED_VIDEO_SOURCES` | 102–106 | `from app.services import studio` (import block) and, after the frozenset literal, `_SUPPORTED_VIDEO_SOURCES = _SUPPORTED_VIDEO_SOURCES \| studio.VIDEO_SOURCES` (literal is not edited) | +5 |
| H3 | `task._run_pipeline` preflight | after the ffmpeg check, ~1723 | `err = studio.preflight(params, stop_at)` → `_mark_task_failed(task_id, "preflight", err)`. Validates preset files, libass, subtitles enabled for scene mode, footage folder, etc. before any paid API call | +6 |
| H4 | `task.get_video_materials` | top of function, ~738 | `if params.video_source in studio.VIDEO_SOURCES: return studio.prepare_materials(task_id, params, audio_duration, audio_file, subtitle_path, video_script)` — builds `scenes.json`/images/clips (scene) or background segment + title card + captions (story) and returns the list of produced files. Script, audio and `subtitle.srt` are read from the task dir (see note below) | +4 |
| H5 | `task.generate_final_videos`, combine step | ~1071 | `_combine_videos = studio.combine_override(params, video.combine_videos)`; the existing call `video.combine_videos(` becomes `_combine_videos(`. For non-studio sources `combine_override` returns the original function, so behaviour is unchanged. For studio sources it returns a function with the same signature that places the pre-rendered video at `combined_video_path` | +3, 1 line modified |
| H6 | `task.generate_final_videos`, after `video.generate_video(...)` | ~1151 | `studio.postprocess_final(task_id, params, index, combined_video_path, audio_file, subtitle_path, final_video_path)` — if a preset with `post.json` is active: `final-N.mp4` → `final-N.raw.mp4`, post-processed result → `final-N.mp4`. Logs that MPT's own subtitle burn-in and BGM are bypassed. On failure it restores the raw file and records a warning rather than failing the task | +4 |
| H7 | `cli.py` | `_CLI_VIDEO_SOURCES` (40) and arg parsing | Extend the source tuple after its literal (`_CLI_VIDEO_SOURCES += studio.VIDEO_SOURCES_TUPLE`) and `studio.cli.add_arguments(parser)` / `studio.cli.apply(args, params)` | ~+6 |
| H8 | `webui/Main.py` | ~148 and ~6029 and the main render area | After `VIDEO_SOURCE_GROUPS` literal: add a `"studio"` group entry; add 2 labels to `video_source_labels`; one call `studio_panel.render(params, tr)` in the right-hand settings column | ~+8 |

Net expected upstream diff: ~45 added lines, 1 modified line, across `schema.py`, `task.py`, `cli.py`, `webui/Main.py`, and `config.example.toml` (a documented `[studio]` section appended at the end). Phase 4 will report the real numbers.

**Note on H4 and signatures.** `get_video_materials(task_id, params, video_terms, audio_duration, ...)` does not receive the script or audio path, but both are in the task directory (`audio.mp3`, `subtitle.srt`, `script.json`), and `utils.task_dir(task_id)` is available. Studio reads from there, so no upstream signature changes.

**Things deliberately not hooked:**
- Script/terms stages: untouched. Scene mode still lets MPT generate terms (unused, cheap). `stop_at` values other than `video` are unaffected.
- `config.py`, `video.py`, `material.py`, `llm.py`: zero edits. Studio imports their helpers.
- i18n JSON files: zero edits (see Open questions).

---

## 3. New package layout

```
app/services/studio/
  __init__.py        # public hook API: VIDEO_SOURCES, preflight, prepare_materials,
                     #   combine_override, postprocess_final (thin dispatchers, lazy imports)
  config.py          # [studio] section loader (config._cfg.get("studio")), defaults, validation
  presets.py         # preset discovery + load + validation (style.json, post.json), path resolution
  ffmpeg_util.py     # run_ffmpeg, libass check, filter-arg/path escaping (Windows drive letters), probe duration
  srt.py             # SRT parser (BOM, , / . ms, multi-line) + time formatting
  ass.py             # ASS generation (styles, escaping {}\ , colors BGR, alignment, letter spacing)
  post.py            # Phase 1: build filter graph + command, cue resolution, dry-run, `python -m` CLI
  scenes.py          # Phase 2: segmentation, scenes.json read/write, orchestration, re-render CLI
  scene_prompts.py   # Phase 2: LLM shot descriptions (JSON, validate, retry once), entity substitution
  image_backends.py  # Phase 2: openai (reuses material helpers) + ComfyUI; hash cache
  motion.py          # Phase 2: zoom/pan filter expressions, per-scene motion picker
  story.py           # Phase 3: orchestration (segment -> title card -> captions -> combined-1.mp4)
  footage.py         # Phase 3: segment picker + used-range JSON log
  titlecard.py       # Phase 3: Pillow title card (own design)
  captions.py        # Phase 3: faster-whisper (lazy), alignment to script, chunking, ASS karaoke
  cli.py             # Phase 4: argparse additions + `python -m app.services.studio` subcommands
  i18n/en.json pl.json   # Phase 4: panel strings (own loader, English fallback)
webui/studio_panel.py    # Phase 4
presets/example/{style.json,post.json,sounds/,luts/,fonts/}   # placeholders only
test/services/test_studio_*.py
```

Dependency direction: `__init__` → everything else; modules never import `task.py`. Imports of `video`, `material`, `llm` happen lazily inside functions so importing `studio` is cheap and has no cycles with `task.py`.

---

## 4. Rendering design notes

**Path/filter escaping** (`ffmpeg_util.py`): filter arguments get the two-level escaping (`\` → `/`, `:` → `\:` so `C\:/x/y`, `'` → `\'`, `,`/`[`/`]`/`;` escaped when inside filtergraph text). To sidestep most of it the command writes the filtergraph to a temp file passed with `-/filter_complex` (ffmpeg ≥ 7; falls back to `-filter_complex_script` on older builds), and paths are passed to `subtitles=`/`ass=`/`lut3d=` as `fontsdir`/`filename` options that are escaped by one helper with unit tests including `C:\Users\me\a b\x.cube`.

**libass check**: `ffmpeg -filters` must list `ass`/`subtitles`; if missing → `logger.error("ffmpeg at <path> was built without libass; subtitle burn-in needs an ffmpeg with --enable-libass …")` and post-processing is skipped (raw video kept).

**Post-processing filter order (documented, fixed)**:
`[video] → scale/format → lut3d → eq → gblur → rgbashift → [subtitles if under_grain] → noise(grain, allf=t) → vignette → cue visuals (invert / flash / black / glitch shift, via enable=between(t,a,b)) → [subtitles if not under_grain] → format=yuv420p`.
Audio: narration (`voice_volume`, `voice_fx`, silence cues via `volume=0:enable=…`), BGM (`aloop`/`-stream_loop`, volume, fade-out ending at narration end), SFX (`adelay` + volume) → `amix=normalize=0` → `alimiter`. Output `libx264 -preset veryfast -crf 18 -pix_fmt yuv420p`, AAC, `+faststart`, `-t <narration duration>`.

**Cue resolution**: `match` is a case-insensitive regex over each SRT line's text; `once` stops after the first hit; `anchor: start|end` selects which edge of the line the time refers to; `offset` seconds is added; `at` is fixed seconds. A cue can combine sfx, glitch, invert/flash/black and `silence_before`. Dry-run prints the resolved events table and the full ffmpeg command, and runs nothing.

**Scene mode**: scenes tile the audio exactly (scene N ends where N+1 starts; last ends at audio end; first starts at 0 even if the first SRT line starts late). Each scene is rendered with ffmpeg `zoompan`/`crop` expressions at 30 fps, then concatenated in order into `combined-1.mp4`. Image cache key = `sha256(prompt|negative|seed|WxH|backend)`, stored in `<task>/scene_images/`.

**Story mode**: one background segment of exactly audio length (`-ss <random> -t <dur>`), scaled+cropped to the target aspect, game audio muted (or `volume`), title card overlaid via `overlay=enable='between(t,0,N)'` with a fade. Captions: faster-whisper word timestamps → aligned to the script text (difflib-style sequence alignment; recognised words take their timing, script words supply the text) → chunks of 1–3 words → ASS with per-word highlight. When post-processing is **active**, story mode writes `captions.ass` and `post.py` burns that instead of the SRT-derived ASS (so `under_grain` still works and `post.json` `subtitles` are ignored). When post-processing is **off**, captions are burned into `combined-1.mp4` directly.

**Title card is original**: rounded panel, channel name, optional avatar, title, optional label; no platform UI, logos, usernames, vote counts or award icons.

---

## 5. Config schemas

### `config.toml` / `config.example.toml` → `[studio]`

```toml
[studio]
presets_dir = "presets"          # relative to repo root
default_preset = "example"       # used when VideoParams.studio_preset is empty (UI/CLI prefill and post-processing)
image_backend = "openai"         # "openai" (reuses [app] openai_image_*) or "comfyui"
comfyui_url = "http://127.0.0.1:8188"
comfyui_timeout = 300
min_scene_seconds = 2.0
max_scene_seconds = 5.0
scene_motions = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static"]
scene_motion_order = "cycle"     # "cycle" or "random" (seeded)
footage_dir = ""                 # story mode background videos
voice_game_audio_volume = 0.0    # story: background footage volume (0 = muted)
title_card_seconds = 3.0
whisper_model = "small"          # faster-whisper model size
whisper_device = "auto"          # "auto" (CUDA if available), "cuda", "cpu"
max_part_seconds = 0             # 0 = no splitting
x264_preset = "veryfast"
x264_crf = 18
```

### `presets/<name>/style.json` (scene mode)

```json
{
  "style_suffix": "…", "negative_prompt": "…",
  "entities": { "Name": "fixed visual description" },
  "seed_mode": "fixed", "base_seed": 12345,
  "width": 1080, "height": 1920,
  "comfyui_workflow": "workflow.json"
}
```
`seed_mode`: `fixed` (base_seed everywhere), `per_scene` (base_seed + index), `random`.

### `presets/<name>/post.json`

`look{lut, contrast, brightness, saturation, gamma, blur, chroma_offset, vignette, grain}`, `subtitles{enabled, font, fonts_dir, size, color, outline_color, outline_width, shadow, uppercase, position, margin_v, letter_spacing, under_grain}`, `voice_volume`, `voice_fx`, `bgm{file, volume, fade_out}`, `cues[{match|at, once, anchor, offset, sfx, volume, glitch, glitch_style, invert, flash, black, silence_before}]`, `x264{preset, crf}`.

### Story section (in `style.json`, key `story`, or a separate `story.json`)

`title_card{background_color, opacity, radius, channel_name, avatar, font, title_size, colors, label}`, `captions{font, size, color, highlight_color, outline, max_words}`.

Validation on load (all presets): unknown keys → error naming the file and key (with a "did you mean" suggestion), wrong types → error with the expected type, referenced files (lut, sfx, bgm, font, avatar, workflow) must exist, relative paths resolve against the preset folder. All errors are collected and reported together.

---

## 6. Phase plan

| Phase | Deliverable | Commit |
|---|---|---|
| 1 | `ffmpeg_util`, `srt`, `ass`, `presets` (post part), `post` + CLI, hooks H1, H2 (sources not yet), H6, `[studio]` + `presets/example`; tests | `feat(studio): post-processing` |
| 2 | scenes/prompts/backends/motion, hooks H2–H5, scenes.json + re-render CLI; tests | `feat(studio): scene mode` |
| 3 | footage/titlecard/captions/story, split_parts; tests | `feat(studio): story mode` |
| 4 | `webui/studio_panel.py`, CLI hook H7, H8, finish docs (this file becomes the final reference with a "rebasing on upstream" table) | `feat(studio): ui, cli, docs` |

After each phase: `uv run python -X utf8 -m pytest -q test`, then commit. I have not run the baseline suite yet (Phase 0 makes no code changes); I will run it first thing in Phase 1 so any pre-existing failure is recorded before my changes.

---

## 7. Risks and decisions to confirm

See the questions in the review message. In short: Polish/i18n approach, story-mode burn-in duplication, `split_parts` vs `video_count`, and whether to skip MoviePy's render when post-processing is active.
