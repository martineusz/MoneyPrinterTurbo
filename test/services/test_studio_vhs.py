"""Analog / VHS look effects in post.json (single ffmpeg pass)."""

import itertools
import os
import subprocess

import pytest
from PIL import Image

from app.services.studio import ffmpeg_util, post, presets

from .studio_helpers import ff, make_preset, make_task, use_presets_dir

TS = {"format": "PLAY  %H:%M:%S", "start": "01:02:03", "position": "bottom_right", "size": 30,
      "color": "#FFEEAA"}
EFFECT_VALUES = {"scanlines": 0.4, "chroma_bleed": 4, "jitter": 0.5, "tracking_bar": True,
                 "fps": 15, "frame_43": True, "timestamp": TS}


def _graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_look_effect_schema_validation(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path)
    make_preset(tmp_path, "good", {"look": dict(EFFECT_VALUES)})
    assert presets.load("good").post["look"]["timestamp"]["start"] == "01:02:03"
    make_preset(tmp_path, "bad", {"look": {
        "scanlines": 2, "chroma_bleed": -1, "jitter": "x", "tracking_bar": 1, "fps": 0,
        "frame_43": "yes", "timestamp": {"format": "it's", "start": "25:99", "position": "middle",
                                         "font": "nope.ttf", "color": "red", "extra": 1}}})
    with pytest.raises(presets.PresetError) as err:
        presets.load("bad")
    text = str(err.value)
    for expected in ("look.scanlines", "look.chroma_bleed", "look.jitter", "look.tracking_bar",
                     "look.fps", "look.frame_43", "must not contain", "use HH:MM:SS or 'random'",
                     "must be one of top_left, bottom_right", "file not found", "invalid color",
                     "unknown key 'extra'"):
        assert expected in text, expected
    make_preset(tmp_path, "rnd", {"look": {"timestamp": {"start": "random"}}})
    assert presets.load("rnd").post["look"]["timestamp"]["start"] == "random"


def _combo_names():
    names = list(EFFECT_VALUES)
    return [c for r in range(len(names) + 1) for c in itertools.combinations(names, r)]


@pytest.fixture(scope="module")
def tiny_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("tiny")
    folder = make_task(str(root / "task"), video_seconds=1, audio_seconds=0.5, size="90x160",
                       srt="1\n00:00:00,000 --> 00:00:00,500\nhi\n")
    preset = presets.Preset(name="t", root=str(root), post={"subtitles": {"enabled": False}})
    return folder, preset


@pytest.mark.parametrize("combo", _combo_names(), ids=lambda c: "+".join(c) or "none")
def test_every_effect_combination_builds_and_renders(tiny_env, combo):
    folder, preset = tiny_env
    preset.post["look"] = {name: EFFECT_VALUES[name] for name in combo}
    plan = post.build_plan(folder, preset, output=os.path.join(folder, "combo.mp4"), write_files=False)
    graph = _graph(plan.cmd)
    assert ("overlay=" in graph) == ("tracking_bar" in combo)
    assert ("drawgrid" in graph) == ("scanlines" in combo)
    assert ("drawtext" in graph) == ("timestamp" in combo)
    assert ("fps=15" in graph) == ("fps" in combo)
    assert len(plan.effects) == len(combo)
    post.run_plan(plan)
    info = ffmpeg_util.probe(plan.output)
    assert (info.width, info.height) == (90, 160) and abs(info.duration - 0.5) < 0.15


def test_effect_order_in_graph(tiny_env):
    folder, preset = tiny_env
    preset.post["look"] = {**EFFECT_VALUES, "grain": 5, "vignette": 0.3}
    graph = _graph(post.build_plan(folder, preset, write_files=False).cmd)
    order = ["crop=90:66", "crop=w=iw-", "gblur=sigma=4", "[base]", "drawgrid", "drawtext",
             "noise=alls=5", "vignette", "fps=15", "format=yuv420p[vout]"]
    positions = [graph.index(token) for token in order]
    assert positions == sorted(positions), dict(zip(order, positions))


def test_timestamp_filter_text_and_random_start_is_stable():
    ts = {"format": "REC %H:%M:%S", "start": "00:10:00"}
    out = post._timestamp_filter(ts, 600, "C:/f.ttf", 1080, 1920, (0, 1920))
    assert "text='%{pts\\:gmtime\\:600\\:REC %H\\\\\\:%M\\\\\\:%S}'" in out and "x=54:y=54" in out
    corner = post._timestamp_filter({"position": "bottom_right"}, 0, "C:/f.ttf", 1080, 1920, (400, 1500))
    assert "x=w-text_w-54:y=1446-text_h" in corner
    a = post.timestamp_offset({"start": "random"}, "taskA-1")
    assert a == post.timestamp_offset({"start": "random"}, "taskA-1") and 0 <= a < 86400
    assert a != post.timestamp_offset({"start": "random"}, "taskB-1")
    assert post.timestamp_offset({"start": "01:02:03"}, "x") == 3723


def test_dry_run_lists_effects(tmp_path, monkeypatch, capsys):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "vhs",
                {"look": {**EFFECT_VALUES, "timestamp": {"start": "random"}}})
    folder = make_task(str(tmp_path / "task"))
    assert post.main([folder, "--preset", "vhs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "look effects:" in out and "scanlines: 0.4" in out and "chroma_bleed: 4" in out
    assert "tracking_bar" in out and "frame_43" in out and "fps: 15" in out and "timestamp:" in out
    assert not os.path.exists(os.path.join(folder, "final-1.mp4"))


def test_smoke_all_effects_on_with_subtitles_and_cues(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "vhs", {
        "look": {**EFFECT_VALUES, "grain": 12, "vignette": 0.5, "contrast": 1.1, "saturation": 1.3},
        "subtitles": {"under_grain": True, "size": 40},
        "cues": [{"match": "wait", "glitch": 0.3}]})
    folder = make_task(str(tmp_path / "task"), video_seconds=6, audio_seconds=5)
    out = post.apply_to_final(folder, presets.load("vhs"))
    info = ffmpeg_util.probe(out)
    assert (info.width, info.height) == (360, 640) and abs(info.duration - 5.0) < 0.2
    stderr = subprocess.run([ffmpeg_util.get_ffmpeg(), "-i", out], capture_output=True, text=True).stderr
    assert " 15 fps" in stderr
    frame = str(tmp_path / "frame.png")
    ff("-ss", "1", "-i", out, "-frames:v", "1", frame)
    image = Image.open(frame).convert("L")
    # 4:3 box inside the 9:16 frame: the bar above the picture is black
    assert max(image.getpixel((x, 20)) for x in range(100, 260)) < 30
    assert image.getpixel((180, 320)) > 20
