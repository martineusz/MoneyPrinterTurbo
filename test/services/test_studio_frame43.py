"""look.frame_43: scene mode fills the 4:3 box that post-processing keeps."""

import io
import json
import os

import pytest
from PIL import Image

from app.services.studio import config as studio_config
from app.services.studio import ffmpeg_util, geometry, post, presets, scenes
from app.services.studio.srt import parse_srt

from .studio_helpers import ff, make_preset, make_task, use_presets_dir

SRT = "1\n00:00:00,000 --> 00:00:03,000\nOne scene\n"


def banded_png(size=(160, 120)):
    """4:3 image: red band on the top rows, blue band on the bottom rows, green in between."""
    image = Image.new("RGB", size, (0, 200, 0))
    for y in range(8):
        for x in range(size[0]):
            image.putpixel((x, y), (255, 0, 0))
            image.putpixel((x, size[1] - 1 - y), (0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class BandBackend:
    name = "bands"

    def fingerprint(self):
        return "v1"

    def generate(self, prompt, negative, seed, width, height):
        return banded_png()


def test_box_geometry_helper_values():
    assert geometry.frame43_box(1080, 1920) == (554, 810)
    assert geometry.frame43_box(360, 640) == (184, 270)
    assert geometry.frame43_box(1920, 1080) is None  # landscape: nothing to box
    assert geometry.frame43_box(1080, 1080) == (134, 810)  # square still has room for 4:3
    top, box_h = geometry.frame43_box(1080, 1920)
    assert top % 2 == 0 and box_h % 2 == 0 and top + box_h <= 1920


def test_post_and_scenes_use_the_same_box(tmp_path):
    top, box_h = geometry.frame43_box(360, 640)
    steps = post._video_chain({"look": {"frame_43": True}}, [], "", None, 0.0, (360, 640))
    assert f"crop=360:{box_h}:0:{top},pad=360:640:0:{top}:black" in steps
    vf = scenes.motion.zoompan_filter("static", 30, 360, box_h, canvas=(360, 640, top))
    assert f"s=360x{box_h}" in vf and f"pad=360:640:0:{top}:black" in vf
    # and the clip scenes render really has its picture in exactly that band
    image = tmp_path / "i.png"
    image.write_bytes(banded_png())
    clip = str(tmp_path / "c.mp4")
    scenes.render_clip(str(image), "static", 10, 360, 640, clip, box=(top, box_h))
    ff("-i", clip, "-frames:v", "1", str(tmp_path / "f.png"))
    frame = Image.open(tmp_path / "f.png").convert("RGB")
    assert frame.getpixel((180, top - 3)) == (16, 16, 16) or max(frame.getpixel((180, top - 3))) < 40
    assert max(frame.getpixel((180, top + box_h + 2))) < 40
    assert frame.getpixel((180, top + 2))[0] > 200  # red band starts at the top of the box
    assert frame.getpixel((180, top + box_h - 3))[2] > 200  # blue band ends at its bottom


def test_wants_frame43_needs_post_and_the_flag():
    from types import SimpleNamespace

    preset = presets.Preset(name="p", root=".", post={"look": {"frame_43": True}})
    assert scenes.wants_frame43(SimpleNamespace(studio_post=True), preset) is True
    assert scenes.wants_frame43(SimpleNamespace(studio_post=False), preset) is False
    assert scenes.wants_frame43(SimpleNamespace(studio_post=True),
                                presets.Preset(name="p", root=".", post={"look": {}})) is False
    assert scenes.wants_frame43(SimpleNamespace(studio_post=True),
                                presets.Preset(name="p", root=".", post=None)) is False


def test_scene_image_survives_the_frame43_crop_end_to_end(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "f", {"look": {"frame_43": True}, "subtitles": {"enabled": False}},
                style={"width": 160, "height": 120, "seed_mode": "fixed"})
    preset = presets.load("f")
    folder = make_task(str(tmp_path / "task"), video_seconds=1, audio_seconds=3, srt=SRT, size="360x640")
    cfg = {**studio_config.get(), "scene_motions": ["static"]}
    ask = lambda p: json.dumps({"scenes": ["x"]})  # noqa: E731
    doc = scenes.build_doc("s", parse_srt(SRT), 3.0, preset, cfg, "9:16", ask=ask, frame_43=True)
    doc["width"], doc["height"] = 360, 640  # keep the test render small
    assert doc["frame_43"] is True
    out = scenes.render(folder, doc, preset, cfg, BandBackend(), image_dir=str(tmp_path / "img"))
    os.replace(out, os.path.join(folder, "combined-1.mp4"))
    final = post.apply_to_final(folder, preset)
    ff("-ss", "1", "-i", final, "-frames:v", "1", str(tmp_path / "final.png"))
    frame = Image.open(tmp_path / "final.png").convert("RGB")
    top, box_h = geometry.frame43_box(360, 640)
    # both edge bands of the original image are still visible after post-processing
    assert frame.getpixel((180, top + 3))[0] > 180 and frame.getpixel((180, top + 3))[2] < 90
    assert frame.getpixel((180, top + box_h - 4))[2] > 180 and frame.getpixel((180, top + box_h - 4))[0] < 90
    assert max(frame.getpixel((180, 20))) < 40 and max(frame.getpixel((180, 620))) < 40  # black bars
    # the doc keeps the flag so a re-render keeps the same geometry
    assert json.load(open(os.path.join(folder, "scenes.json"), encoding="utf-8"))["frame_43"] is True


def test_without_frame43_the_scene_is_full_frame(tmp_path):
    image = tmp_path / "i.png"
    image.write_bytes(banded_png((120, 160)))
    clip = str(tmp_path / "c.mp4")
    scenes.render_clip(str(image), "static", 5, 360, 640, clip)
    info = ffmpeg_util.probe(clip)
    assert (info.width, info.height) == (360, 640)


def test_validate_doc_rejects_non_boolean_frame43(tmp_path):
    doc = {"audio_duration": 3.0, "frame_43": "yes", "scenes": [
        {"index": 1, "start": 0, "end": 3.0, "narration": "n", "prompt": "p", "seed": 1,
         "motion": "static"}]}
    with pytest.raises(scenes.SceneError, match="frame_43"):
        scenes.validate_doc(doc)
    doc["frame_43"] = True
    scenes.validate_doc(doc)


def test_style_size_warning(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    post_json = {"look": {"frame_43": True}}
    portrait = presets.Preset(name="p", root=".", post=post_json, style={"width": 768, "height": 1344})
    assert "1152x864" in presets.frame43_style_warning(portrait) and "768x1344" in presets.frame43_style_warning(portrait)
    unset = presets.Preset(name="p", root=".", post=post_json, style={})
    assert "unset" in presets.frame43_style_warning(unset)
    for good in ({"width": 1152, "height": 864}, {"width": 1024, "height": 768}):
        assert presets.frame43_style_warning(
            presets.Preset(name="p", root=".", post=post_json, style=good)) == ""
    assert presets.frame43_style_warning(
        presets.Preset(name="p", root=".", post={"look": {}}, style={"width": 768, "height": 1344})) == ""
    assert presets.frame43_style_warning(presets.Preset(name="p", root=".", post=post_json, style=None)) == ""


def test_loading_a_mismatched_preset_logs_the_warning_once(tmp_path, monkeypatch):
    from loguru import logger

    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "warnme", {"look": {"frame_43": True}},
                style={"width": 768, "height": 1344})
    seen = []
    handler = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    try:
        presets.load("warnme")
        presets.load("warnme")
    finally:
        logger.remove(handler)
    assert sum("not 4:3 landscape" in m for m in seen) == 1
