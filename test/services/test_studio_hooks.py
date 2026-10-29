"""Hook points: with studio off nothing changes; with it on the preset is applied."""

import os

from app.models.schema import VideoParams
from app.services import studio
from app.services.studio import ffmpeg_util

from .studio_helpers import make_preset, make_task, use_presets_dir


def _params(**kwargs):
    return VideoParams(video_subject="x", **kwargs)


def test_studio_is_off_by_default():
    params = _params()
    assert params.studio_post is False and params.studio_preset == ""
    sentinel = object()
    assert studio.active_preset_name(params) == ""
    assert studio.render_override(params, sentinel) is sentinel
    assert studio.combine_override(params, sentinel) is sentinel
    assert studio.preflight(params, "video") == ""


def test_postprocess_final_is_noop_when_off(tmp_path):
    final = tmp_path / "final-1.mp4"
    final.write_bytes(b"original")
    studio.postprocess_final("t", _params(), 1, "c", "a", "s", str(final))
    assert final.read_bytes() == b"original"
    assert not (tmp_path / "final-1.raw.mp4").exists()


def test_preflight_reports_missing_preset_and_missing_post_json(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path)
    assert "preset folder not found" in studio.preflight(_params(studio_post=True, studio_preset="nope"), "video")
    make_preset(tmp_path, "nopost", None, files={"notes.txt": "x"})
    assert "no post.json" in studio.preflight(_params(studio_post=True, studio_preset="nopost"), "video")
    assert studio.preflight(_params(studio_post=True, studio_preset="nopost"), "script") == ""


def test_postprocess_final_applies_preset_and_survives_failure(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "p", {"look": {"grain": 4}, "subtitles": {"enabled": False}})
    folder = make_task(str(tmp_path / "task"))
    final = os.path.join(folder, "final-1.mp4")
    with open(final, "wb") as handle:
        handle.write(b"original")
    params = _params(studio_post=True, studio_preset="p")
    studio.postprocess_final("t", params, 1, "", "", "", final)
    assert open(os.path.join(folder, "final-1.raw.mp4"), "rb").read() == b"original"
    assert abs(ffmpeg_util.probe(final).duration - 5.0) < 0.2

    # A broken preset must leave the existing final video in place.
    broken = _params(studio_post=True, studio_preset="missing")
    before = open(final, "rb").read()
    studio.postprocess_final("t", broken, 1, "", "", "", final)
    assert open(final, "rb").read() == before


def test_skip_mpt_render_muxes_only(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets", skip_mpt_render=True)
    folder = make_task(str(tmp_path / "task"))
    params = _params(studio_post=True, studio_preset="p")
    render = studio.render_override(params, object())
    out = os.path.join(folder, "final-1.mp4")
    assert render(video_path=os.path.join(folder, "combined-1.mp4"),
                  audio_path=os.path.join(folder, "audio.mp3"), subtitle_path="",
                  output_file=out, params=params, bgm_file_override=None) is True
    assert abs(ffmpeg_util.probe(out).duration - 5.0) < 0.3
