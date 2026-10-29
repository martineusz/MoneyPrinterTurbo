"""CLI and WebUI hook points for studio."""

import cli
from app.config import config as app_config


def parse(argv):
    return cli.parse_args(["--video-subject", "x", *argv])


def test_cli_accepts_studio_sources_and_options(monkeypatch):
    monkeypatch.setattr(app_config, "ui", {})
    args = parse(["--video-source", "studio_story", "--studio-preset", "p", "--studio-post",
                  "--studio-story-title", "T", "--studio-footage-dir", "/f", "--studio-split-parts"])
    params = cli.build_video_params(args)
    assert (params.video_source, params.studio_preset, params.studio_post) == ("studio_story", "p", True)
    assert (params.studio_story_title, params.studio_footage_dir) == ("T", "/f")
    assert params.studio_story_split_parts is True


def test_cli_defaults_keep_studio_off(monkeypatch):
    monkeypatch.setattr(app_config, "ui", {})
    params = cli.build_video_params(parse([]))
    assert params.studio_post is False and params.studio_preset == ""
    assert params.video_source == "pexels"


def test_webui_renders_studio_panel_without_errors():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file("webui/Main.py", default_timeout=90)
    app.run()
    assert not app.exception
    assert any(e.label == "Studio" for e in app.expander)
    assert any(c.key == "studio_post_checkbox" for c in app.checkbox)
