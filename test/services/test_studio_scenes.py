import io
import json
import os

import pytest
from PIL import Image

from app.services.studio import config as studio_config
from app.services.studio import ffmpeg_util, image_backends, motion, presets, scene_prompts, scenes
from app.services.studio.segmentation import check_tiling, segment
from app.services.studio.srt import Cue, parse_srt

from .studio_helpers import make_preset, make_task, use_presets_dir


def cues(*spans):
    return [Cue(i + 1, a, b, f"line {i + 1} words here") for i, (a, b) in enumerate(spans)]


def assert_tiles(segs, audio):
    assert segs[0].start == 0
    for left, right in zip(segs, segs[1:]):
        assert left.end == right.start
    assert segs[-1].end == audio


# ---- segmentation ---------------------------------------------------------------

def test_segments_tile_timeline_with_gaps_and_late_start():
    segs = segment(cues((1.0, 3.0), (4.0, 7.0), (9.0, 12.0)), 13.0)
    assert_tiles(segs, 13.0)
    assert len(segs) == 3  # 1-4, 4-9 (5.0, not split), 9-13


def test_short_lines_merge_with_next_and_trailing_short_with_previous():
    segs = segment(cues((0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 5.5)), 5.5)
    assert_tiles(segs, 5.5)
    assert [(s.start, s.end) for s in segs] == [(0, 2.0), (2.0, 5.5)]
    assert "line 1" in segs[0].text and "line 2" in segs[0].text
    assert "line 4" in segs[-1].text


def test_long_line_is_split_evenly_and_text_is_distributed():
    long = [Cue(1, 0, 14.0, "one two three four five six seven eight nine ten")]
    segs = segment(long, 14.0)
    assert_tiles(segs, 14.0)
    assert len(segs) == 3
    assert all(s.end - s.start <= 5.0 + 1e-6 for s in segs)
    assert " ".join(s.text for s in segs) == long[0].text


def test_single_line_and_single_short_line():
    assert_tiles(segment([Cue(1, 0.5, 3.0, "hello there")], 3.0), 3.0)
    only = segment([Cue(1, 0, 1.0, "tiny")], 1.0)
    assert len(only) == 1 and (only[0].start, only[0].end) == (0, 1.0)


def test_cues_beyond_audio_are_dropped_and_bad_arguments_rejected():
    segs = segment(cues((0, 3.0), (3.0, 6.0), (20.0, 25.0)), 6.0)
    assert_tiles(segs, 6.0)
    with pytest.raises(ValueError):
        segment([], 5.0)
    with pytest.raises(ValueError):
        segment(cues((0, 3.0)), 3.0, min_seconds=6, max_seconds=5)


@pytest.mark.parametrize("seed", range(20))
def test_random_timelines_always_tile(seed):
    import random
    rng = random.Random(seed)
    t, spans = rng.random() * 2, []
    for _ in range(rng.randint(1, 25)):
        length = rng.choice([0.4, 1.0, 2.5, 4.0, 8.0, 12.0]) * (0.5 + rng.random())
        spans.append((t, t + length))
        t += length + rng.choice([0, 0, 0.3, 2.0])
    audio = t + rng.random()
    segs = segment(cues(*spans), audio)
    assert_tiles(segs, round(audio, 3))
    assert all(s.end > s.start for s in segs)
    assert all(s.end - s.start <= 5.0 + 1e-3 for s in segs)


def test_check_tiling_reports_gap_overlap_and_end():
    good = [{"index": 1, "start": 0, "end": 2}, {"index": 2, "start": 2, "end": 4}]
    check_tiling(good, 4)
    with pytest.raises(ValueError, match="gap"):
        check_tiling([good[0], {"index": 2, "start": 2.5, "end": 4}], 4)
    with pytest.raises(ValueError, match="overlap"):
        check_tiling([good[0], {"index": 2, "start": 1.5, "end": 4}], 4)
    with pytest.raises(ValueError, match="audio lasts"):
        check_tiling(good, 5)


# ---- prompts ---------------------------------------------------------------------

def test_parse_response_accepts_fences_lists_and_numbered_objects():
    fenced = "```json\n{\"scenes\": [{\"scene\": 1, \"prompt\": \"a cat\"}, {\"scene\": 2, \"prompt\": \"a dog\"}]}\n```"
    assert scene_prompts.parse_response(fenced, 2) == ["a cat", "a dog"]
    assert scene_prompts.parse_response('["a  cat", "a dog"]', 2) == ["a cat", "a dog"]
    assert scene_prompts.parse_response('Sure! {"scenes": ["x"]} done', 1) == ["x"]


@pytest.mark.parametrize("text", [
    "not json", '{"scenes": "x"}', '{"scenes": ["a"]}', '{"scenes": ["a", ""]}',
    '{"scenes": [{"scene": 2, "prompt": "a"}, {"scene": 1, "prompt": "b"}]}',
    '{"scenes": [1, 2]}',
])
def test_parse_response_rejects_malformed(text):
    with pytest.raises(scene_prompts.ScenePromptError):
        scene_prompts.parse_response(text, 2)


def test_generate_prompts_retries_once_with_the_problem_in_the_prompt():
    seen = []

    def ask(prompt):
        seen.append(prompt)
        return "garbage" if len(seen) == 1 else '{"scenes": ["Mara walks", "a road"]}'

    out = scene_prompts.generate_prompts("script", ["a", "b"], {"Mara": "a tall woman in red"}, ask=ask)
    assert out == ["a tall woman in red walks", "a road"]
    assert len(seen) == 2 and "rejected (response is not valid JSON)" in seen[1]
    assert "Mara" in seen[0] and "script" in seen[0]


def test_generate_prompts_gives_up_after_second_failure_and_reports_provider_errors():
    with pytest.raises(scene_prompts.ScenePromptError, match="twice"):
        scene_prompts.generate_prompts("s", ["a"], {}, ask=lambda p: "nope")
    with pytest.raises(scene_prompts.ScenePromptError, match="quota"):
        scene_prompts.generate_prompts("s", ["a"], {}, ask=lambda p: "Error: quota exceeded")


def test_entity_substitution_is_whole_word_case_insensitive_and_single_pass():
    entities = {"Mara": "a tall woman, MARA-LOOK", "Mara Jones": "full-name look", "Town": "a harbour town"}
    text = "mara and Mara Jones near Town, not Marathon or Townsend"
    assert scene_prompts.substitute_entities(text, entities) == (
        "a tall woman, MARA-LOOK and full-name look near a harbour town, not Marathon or Townsend")
    assert scene_prompts.substitute_entities("x", {}) == "x"


# ---- backends --------------------------------------------------------------------

def test_cache_key_depends_on_every_input():
    base = image_backends.cache_key("p", "n", 1, 10, 20, "openai", "m")
    assert base == image_backends.cache_key("p", "n", 1, 10, 20, "openai", "m")
    for changed in (("q", "n", 1, 10, 20, "openai", "m"), ("p", "x", 1, 10, 20, "openai", "m"),
                    ("p", "n", 2, 10, 20, "openai", "m"), ("p", "n", 1, 11, 20, "openai", "m"),
                    ("p", "n", 1, 10, 21, "openai", "m"), ("p", "n", 1, 10, 20, "comfyui", "m")):
        assert image_backends.cache_key(*changed) != base


def test_fill_workflow_replaces_placeholders_without_mutating_original():
    wf = {"1": {"inputs": {"text": "{prompt}, extra", "seed": "{seed}", "w": "{width}"}},
          "2": {"inputs": {"text": "{negative}", "h": "{height}"}}}
    out = image_backends.fill_workflow(wf, "a \"quoted\" cat", "bad", 7, 512, 768)
    assert out["1"]["inputs"] == {"text": "a \"quoted\" cat, extra", "seed": 7, "w": 512}
    assert out["2"]["inputs"] == {"text": "bad", "h": 768}
    assert wf["1"]["inputs"]["seed"] == "{seed}"


class FakeResponse:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload, self.content, self.status_code, self.text = payload, content, status, ""

    def json(self):
        return self._payload


def png_bytes(color=(200, 30, 30), size=(64, 96)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_comfy_backend_posts_polls_and_downloads(tmp_path):
    workflow = tmp_path / "wf.json"
    workflow.write_text(json.dumps({"1": {"inputs": {"text": "{prompt}", "seed": "{seed}"}}}))
    calls = {"history": 0}

    class Http:
        def post(self, url, json=None, timeout=None):
            assert url == "http://comfy/prompt" and json["prompt"]["1"]["inputs"] == {"text": "hi", "seed": 5}
            return FakeResponse({"prompt_id": "abc"})

        def get(self, url, params=None, timeout=None):
            if url.endswith("/history/abc"):
                calls["history"] += 1
                if calls["history"] < 2:
                    return FakeResponse({})
                return FakeResponse({"abc": {"outputs": {"9": {"images": [{"filename": "o.png"}]}}}})
            assert url == "http://comfy/view" and params["filename"] == "o.png"
            return FakeResponse(content=b"IMG")

    backend = image_backends.ComfyBackend("http://comfy/", str(workflow), timeout=5, http=Http(),
                                          poll_interval=0)
    assert backend.generate("hi", "", 5, 10, 10) == b"IMG"
    assert calls["history"] == 2


def test_comfy_backend_errors(tmp_path):
    with pytest.raises(image_backends.ImageBackendError, match="comfyui_workflow"):
        image_backends.ComfyBackend("http://x", "")
    workflow = tmp_path / "wf.json"
    workflow.write_text("{}")

    class Http:
        def post(self, *a, **k):
            return FakeResponse(status=400)

    with pytest.raises(image_backends.ImageBackendError, match="rejected"):
        image_backends.ComfyBackend("http://x", str(workflow), http=Http()).generate("p", "", 1, 1, 1)


# ---- rendering with a mocked backend ----------------------------------------------

class SolidBackend:
    name = "fake"

    def __init__(self):
        self.calls = []

    def fingerprint(self):
        return "v1"

    def generate(self, prompt, negative, seed, width, height):
        self.calls.append((prompt, negative, seed))
        shade = 40 + 30 * (len(self.calls) % 6)
        return png_bytes((shade, 255 - shade, 90))


STYLE = {"style_suffix": "suffix words", "negative_prompt": "neg", "entities": {"Mara": "a tall woman"},
         "seed_mode": "per_scene", "base_seed": 100, "width": 180, "height": 320}
SRT = ("1\n00:00:00,000 --> 00:00:03,000\nMara arrives\n\n"
       "2\n00:00:03,000 --> 00:00:06,000\nShe leaves town\n\n"
       "3\n00:00:06,000 --> 00:00:09,000\nThe end\n")


@pytest.fixture
def scene_env(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "s", {}, style=STYLE)
    folder = make_task(str(tmp_path / "task"), video_seconds=2, audio_seconds=9, srt=SRT)
    cfg = studio_config.get()
    preset = presets.load("s")
    asks = []

    def ask(prompt):
        asks.append(prompt)
        return json.dumps({"scenes": [{"scene": i, "prompt": f"Mara shot {i}"} for i in (1, 2, 3)]})

    doc = scenes.build_doc("script text", parse_srt(SRT), 9.0, preset, cfg, "9:16", ask=ask)
    assert (doc["width"], doc["height"]) == (1080, 1920)  # video size follows the aspect
    assert (doc["image_width"], doc["image_height"]) == (180, 320)  # style size = image request
    doc["width"], doc["height"] = 180, 320  # keep the test renders small
    return folder, doc, preset, cfg, tmp_path / "images"


def test_build_doc_fields_seeds_and_motions(scene_env):
    _, doc, *_ = scene_env
    assert [s["seed"] for s in doc["scenes"]] == [101, 102, 103]
    assert [s["motion"] for s in doc["scenes"]] == ["zoom_in", "zoom_out", "pan_left"]
    assert doc["scenes"][0]["prompt"] == "a tall woman shot 1"
    scenes.validate_doc(doc)


def test_render_tiles_audio_caches_images_and_regenerates_selected(scene_env):
    folder, doc, preset, cfg, images = scene_env
    backend = SolidBackend()
    out = scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    info = ffmpeg_util.probe(out)
    assert (info.width, info.height) == (180, 320)
    assert abs(info.duration - (9.0 + 0.1)) < 0.15
    assert len(backend.calls) == 3
    assert backend.calls[0][0] == "a tall woman shot 1, suffix words" and backend.calls[0][1] == "neg"
    saved = json.load(open(os.path.join(folder, "scenes.json"), encoding="utf-8"))
    assert all(os.path.isfile(s["image"]) for s in saved["scenes"])

    scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    assert len(backend.calls) == 3  # nothing regenerated

    doc["scenes"][1]["prompt"] = "edited prompt"
    doc["scenes"][2]["motion"] = "static"
    scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    assert len(backend.calls) == 4 and backend.calls[-1][0].startswith("edited prompt")

    scenes.render(folder, doc, preset, cfg, backend, regenerate={1}, image_dir=str(images))
    assert len(backend.calls) == 5


@pytest.mark.parametrize("name", motion.MOTIONS)
def test_every_motion_renders_a_clip_of_the_right_length(tmp_path, name):
    image = tmp_path / "i.png"
    image.write_bytes(png_bytes(size=(200, 120)))
    out = str(tmp_path / f"{name}.mp4")
    scenes.render_clip(str(image), name, 45, 108, 192, out)
    info = ffmpeg_util.probe(out)
    assert (info.width, info.height) == (108, 192) and abs(info.duration - 1.5) < 0.1


def test_motion_picker_cycle_random_and_validation():
    assert motion.pick_motions(4, ["zoom_in", "static"], "cycle", [1, 2, 3, 4]) == [
        "zoom_in", "static", "zoom_in", "static"]
    first = motion.pick_motions(5, list(motion.MOTIONS), "random", [1, 2, 3, 4, 5])
    assert first == motion.pick_motions(5, list(motion.MOTIONS), "random", [1, 2, 3, 4, 5])
    with pytest.raises(ValueError):
        motion.pick_motions(1, ["spin"], "cycle", [1])


def test_validate_doc_rejects_gaps_and_unknown_motion(scene_env):
    _, doc, *_ = scene_env
    doc["scenes"][1]["start"] += 0.5
    with pytest.raises(scenes.SceneError, match="gap|overlap"):
        scenes.validate_doc(doc)
    doc["scenes"][1]["start"] -= 0.5
    doc["scenes"][0]["motion"] = "spin"
    with pytest.raises(scenes.SceneError, match="unknown motion"):
        scenes.validate_doc(doc)


def test_rerender_cli_flow_without_final(scene_env, monkeypatch):
    folder, doc, preset, cfg, images = scene_env
    backend = SolidBackend()
    scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    edited = json.load(open(os.path.join(folder, "scenes.json"), encoding="utf-8"))
    edited["scenes"][0]["prompt"] = "brand new shot"
    json.dump(edited, open(os.path.join(folder, "scenes.json"), "w", encoding="utf-8"))
    out = scenes.rerender(folder, {2}, "s", final=False, backend=backend, image_dir=str(images))
    assert out == os.path.join(folder, "combined-1.mp4") and os.path.isfile(out)
    assert len(backend.calls) == 3 + 1 + 1  # edited scene 1 + forced scene 2
    with pytest.raises(scenes.SceneError, match="no such scene"):
        scenes.rerender(folder, {9}, "s", final=False, backend=backend, image_dir=str(images))


def test_build_for_task_end_to_end_and_hook_wiring(tmp_path, monkeypatch):
    from app.models.schema import VideoParams
    from app.services import studio
    from app.utils import utils

    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "s", {}, style=STYLE)
    folder = make_task(str(tmp_path / "t"), video_seconds=2, audio_seconds=9, srt=SRT)
    monkeypatch.setattr(utils, "task_dir", lambda sub="": folder)
    params = VideoParams(video_subject="x", video_source="studio_scenes", studio_preset="s")
    backend = SolidBackend()
    ask = lambda p: json.dumps({"scenes": ["a", "b", "c"]})  # noqa: E731
    paths = scenes.build_for_task("t", params, 9.0, ask=ask, backend=backend,
                                  image_dir=str(tmp_path / "img"))
    assert paths == [os.path.join(folder, "studio-combined.mp4")]

    combined = os.path.join(folder, "combined-1.mp4")
    studio.combine_override(params, object())(
        combined_video_path=combined, video_paths=paths, audio_file="x")
    assert abs(ffmpeg_util.probe(combined).duration - 9.1) < 0.15


def test_prepare_marks_failure_instead_of_raising(tmp_path, monkeypatch):
    from app.models.schema import VideoParams
    from app.services import studio
    from app.utils import utils

    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "s", {}, style=STYLE)
    monkeypatch.setattr(utils, "task_dir", lambda sub="": str(tmp_path))  # no subtitle.srt there
    failures = []
    params = VideoParams(video_subject="x", video_source="studio_scenes", studio_preset="s")
    assert studio.prepare_materials("t", params, 5.0, lambda *a: failures.append(a)) is None
    assert failures and "subtitle.srt" in failures[0][2]


def test_video_source_is_registered_with_the_pipeline():
    from app.services import task

    assert {"studio_scenes", "studio_story"} <= task._SUPPORTED_VIDEO_SOURCES
