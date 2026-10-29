"""style.json: style_prefix and prompt_guidance."""

import json

import pytest

from app.services.studio import config as studio_config
from app.services.studio import presets, scene_prompts, scenes
from app.services.studio.srt import parse_srt

from .studio_helpers import make_preset, use_presets_dir
from .test_studio_scenes import SRT, STYLE, SolidBackend, scene_env  # noqa: F401


def test_style_prefix_suffix_and_guidance_reach_their_places(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    style = {**STYLE, "style_prefix": "PREFIX words", "style_suffix": "SUFFIX words",
             "prompt_guidance": "Always shoot from knee height."}
    make_preset(tmp_path / "presets", "g", {}, style=style)
    preset = presets.load("g")
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return json.dumps({"scenes": ["a", "b", "c"]})

    doc = scenes.build_doc("script", parse_srt(SRT), 9.0, preset, studio_config.get(), "9:16", ask=ask)
    # guidance goes into the LLM instructions, before the JSON-shape request
    assert "Always shoot from knee height." in asked[0]
    assert asked[0].index("Always shoot from knee height.") < asked[0].index("Return ONLY JSON")
    # prefix + LLM description + suffix, in that order, for the image backend
    assert scenes._full_prompt(doc["scenes"][0], preset.style) == "PREFIX words, a, SUFFIX words"
    # the stored (editable) prompt stays free of both
    assert doc["scenes"][0]["prompt"] == "a"


def test_prefix_reaches_the_image_backend_and_changes_the_cache_key(scene_env):  # noqa: F811
    folder, doc, preset, cfg, images = scene_env
    backend = SolidBackend()
    scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    preset.style = {**preset.style, "style_prefix": "NEW PREFIX"}
    scenes.render(folder, doc, preset, cfg, backend, image_dir=str(images))
    assert len(backend.calls) == 6 and backend.calls[3][0].startswith("NEW PREFIX, ")


def test_new_style_keys_are_optional_and_validated(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "ok", {}, style={"style_prefix": "p", "prompt_guidance": "g"})
    assert presets.load("ok").style["prompt_guidance"] == "g"
    make_preset(tmp_path / "presets", "none", {}, style={})
    assert presets.load("none").style == {}
    make_preset(tmp_path / "presets", "bad", {}, style={"style_prefix": 3, "prompt_guidance": ["x"]})
    with pytest.raises(presets.PresetError) as err:
        presets.load("bad")
    assert "style.json.style_prefix" in str(err.value) and "style.json.prompt_guidance" in str(err.value)
    assert "Additional guidance" not in scene_prompts.build_prompt("s", ["a"], [])
