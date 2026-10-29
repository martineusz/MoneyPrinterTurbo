import os
import subprocess

import pytest

from app.services.studio import ass, ffmpeg_util, post, presets
from app.services.studio import config as studio_config
from app.services.studio.srt import parse_srt

from .studio_helpers import IDENTITY_CUBE, ff, make_preset, make_task, use_presets_dir


# ---- SRT -------------------------------------------------------------------

def test_srt_bom_dot_milliseconds_and_multiline():
    text = ("\ufeff1\n00:00:01.5 --> 00:00:02,25\nfirst\nsecond line\n\n"
            "2\r\n00:01:00,000 --> 00:01:01,000\r\nnext\r\n")
    cues = parse_srt(text)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (1.5, 2.25, "first\nsecond line"), (60.0, 61.0, "next")]
    assert [c.index for c in cues] == [1, 2]


def test_srt_skips_empty_and_inverted_blocks():
    text = ("1\n00:00:02,000 --> 00:00:01,000\nbackwards\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n\n")
    assert parse_srt(text) == []


# ---- ASS -------------------------------------------------------------------

def test_ass_color_is_bgr():
    assert ass.ass_color("#FF8000") == "&H000080FF"
    with pytest.raises(ValueError):
        ass.ass_color("red")


def test_ass_escaping_time_and_dialogue():
    assert ass.escape_text("a {b} \\ c\nd", uppercase=True) == "A \\{B\\} \\\\ C\\ND"
    assert ass.ass_time(3725.678) == "1:02:05.68"
    cues = parse_srt("1\n00:00:00,000 --> 00:00:01,000\nhi {x}\n")
    text = ass.build_ass(cues, 1080, 1920, {"position": "top", "font": "Foo", "size": 50,
                                            "letter_spacing": 2, "margin_v": 99})
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "Style: Default,Foo,50," in text
    assert ",100,100,2,0,1,3,0,8,60,60,99,1" in text  # spacing 2, alignment 8, MarginV 99
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,hi \\{x\\}" in text


def test_ass_alignment_values():
    for pos, value in (("bottom", 2), ("middle", 5), ("top", 8)):
        assert f",{value},60,60," in ass.header(100, 100, {"position": pos})


# ---- Escaping --------------------------------------------------------------

def test_quote_filter_value_windows_drive_and_special_chars():
    assert ffmpeg_util.quote_filter_value("C:\\Users\\me\\a b\\x.cube") == "'C\\:/Users/me/a b/x.cube'"
    assert ffmpeg_util.quote_filter_value("it's,here") == "'it'\\\\\\''s,here'"


# ---- Presets ---------------------------------------------------------------

def test_preset_validation_reports_everything(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path)
    make_preset(tmp_path, "bad", {
        "look": {"grain": "lots", "contrats": 1, "lut": "missing.cube"},
        "subtitles": {"position": "left", "color": "red"},
        "cues": [{"match": "(", "glitch": 0.2}, {"at": 1, "match": "x", "sfx": "nope.wav"}, {"at": 2}],
        "surprise": True,
    })
    with pytest.raises(presets.PresetError) as err:
        presets.load("bad")
    text = str(err.value)
    for expected in ("post.json.look.grain", "unknown key 'contrats' (did you mean 'contrast'?)",
                     "file not found", "must be one of top, middle, bottom", "invalid color",
                     "invalid regex", "exactly one of 'match' or 'at'", "cue has no effect",
                     "unknown key 'surprise'"):
        assert expected in text, expected


def test_preset_resolves_relative_paths(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path)
    folder = make_preset(tmp_path, "ok", {"look": {"lut": "luts/a.cube"}},
                         files={"luts/a.cube": IDENTITY_CUBE})
    loaded = presets.load("ok")
    assert loaded.post["look"]["lut"] == os.path.normpath(os.path.join(folder, "luts", "a.cube"))


def test_preset_name_cannot_escape(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path)
    with pytest.raises(presets.PresetError):
        presets.load("../x")


def test_example_preset_is_valid(monkeypatch):
    monkeypatch.setattr(studio_config, "_raw", lambda: {})
    assert presets.load("example").post["look"]["grain"] == 6


# ---- Cue resolution --------------------------------------------------------

def _lines():
    return parse_srt(
        "1\n00:00:01,000 --> 00:00:02,000\nWait here\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nwait again\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nnothing\n")


def test_cue_match_is_case_insensitive_and_hits_every_line():
    events = post.resolve_cues([{"match": "WAIT", "glitch": 0.2}], _lines())
    assert [e.time for e in events] == [1.0, 3.0]


def test_cue_once_anchor_and_offset():
    events = post.resolve_cues([{"match": "wait", "once": True, "anchor": "end", "offset": -0.25,
                                 "flash": 0.1}], _lines())
    assert [e.time for e in events] == [1.75]


def test_cue_at_negative_clamp_and_ordering():
    events = post.resolve_cues([
        {"at": 4.5, "invert": 0.1},
        {"match": "wait", "offset": -5, "black": 0.1, "once": True},
    ], _lines())
    assert [(e.time, e.cue_index) for e in events] == [(0.0, 1), (4.5, 0)]


# ---- Plan + end-to-end -----------------------------------------------------

FULL_POST = {
    "look": {"lut": "luts/id.cube", "contrast": 1.1, "brightness": 0.02, "saturation": 1.1,
             "gamma": 1.0, "blur": 0.5, "chroma_offset": 2, "vignette": 0.3, "grain": 8},
    "subtitles": {"font": "Arial", "size": 40, "uppercase": True, "position": "bottom",
                  "under_grain": True},
    "voice_volume": 1.2,
    "voice_fx": "highpass=f=80",
    "bgm": {"file": "sounds/bgm.wav", "volume": 0.3, "fade_out": 1.0},
    "cues": [
        {"match": "wait", "once": True, "glitch": 0.4, "sfx": "sounds/hit.wav", "volume": 0.8,
         "silence_before": 0.3},
        {"at": 3.9, "invert": 0.1}, {"at": 4.1, "flash": 0.1}, {"at": 4.3, "black": 0.1},
    ],
}


def _preset_with_media(tmp_path, monkeypatch, post_json=None):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "full", post_json or FULL_POST,
                files={"luts/id.cube": IDENTITY_CUBE})
    sounds = tmp_path / "presets" / "full" / "sounds"
    sounds.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "anoisesrc=d=2:c=pink:r=44100", str(sounds / "bgm.wav"))
    ff("-f", "lavfi", "-i", "sine=frequency=880:duration=0.5", str(sounds / "hit.wav"))
    return presets.load("full")


def _graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_dry_run_lists_events_and_does_not_render(tmp_path, monkeypatch):
    _preset_with_media(tmp_path, monkeypatch)
    folder = make_task(str(tmp_path / "task"))
    cmd, events = post.dry_run(folder, "full")
    assert [round(e.time, 2) for e in events] == [2.0, 3.9, 4.1, 4.3]
    graph = _graph(cmd)
    assert "lut3d=file=" in graph and "rgbashift" in graph and "amix=inputs=3" in graph
    assert "normalize=0" in graph and "alimiter" in graph and "volume=0:enable=" in graph
    assert not os.path.exists(os.path.join(folder, "final-1.mp4"))
    assert not os.path.exists(os.path.join(folder, "post-1.ass"))


def test_subtitle_position_in_filter_order(tmp_path, monkeypatch):
    preset = _preset_with_media(tmp_path, monkeypatch)
    folder = make_task(str(tmp_path / "task"))
    graph = _graph(post.build_plan(folder, preset, write_files=False).cmd)
    assert graph.index("ass=filename") < graph.index("noise=alls=8")
    preset.post["subtitles"]["under_grain"] = False
    graph = _graph(post.build_plan(folder, preset, write_files=False).cmd)
    assert graph.index("ass=filename") > graph.index("drawbox")


def test_end_to_end_smoke_with_awkward_paths(tmp_path, monkeypatch):
    preset = _preset_with_media(tmp_path, monkeypatch)
    folder = make_task(str(tmp_path / "it's a task, (x)"))
    with open(os.path.join(folder, "final-1.mp4"), "wb") as handle:
        handle.write(b"mpt original")
    out = post.apply_to_final(folder, preset)
    assert out == os.path.join(folder, "final-1.mp4")
    with open(os.path.join(folder, "final-1.raw.mp4"), "rb") as handle:
        assert handle.read() == b"mpt original"
    info = ffmpeg_util.probe(out)
    assert abs(info.duration - 5.0) < 0.2
    assert (info.width, info.height) == (360, 640)
    stderr = subprocess.run([ffmpeg_util.get_ffmpeg(), "-i", out], capture_output=True, text=True).stderr
    assert "Video: h264" in stderr and "Audio: aac" in stderr
    post.apply_to_final(folder, preset)  # re-run must not overwrite the preserved raw file
    with open(os.path.join(folder, "final-1.raw.mp4"), "rb") as handle:
        assert handle.read() == b"mpt original"


def test_short_video_is_padded_to_narration(tmp_path, monkeypatch):
    preset = _preset_with_media(tmp_path, monkeypatch,
                                {"look": {"grain": 3}, "subtitles": {"enabled": False}})
    folder = make_task(str(tmp_path / "t"), video_seconds=3, audio_seconds=5)
    out = post.apply_to_final(folder, preset)
    assert abs(ffmpeg_util.probe(out).duration - 5.0) < 0.2


def test_cli_dry_run_and_errors(tmp_path, monkeypatch, capsys):
    _preset_with_media(tmp_path, monkeypatch)
    folder = make_task(str(tmp_path / "task"))
    assert post.main([folder, "--preset", "full", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "cue#0" in out and "libx264" in out
    assert post.main([folder, "--preset", "nope"]) == 1
