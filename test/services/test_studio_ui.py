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


def test_webui_source_validation_accepts_studio_sources():
    """The pre-submission check must not reject studio_scenes / studio_story."""
    import ast
    from pathlib import Path

    from app.services import studio

    tree = ast.parse(Path("webui/Main.py").read_text(encoding="utf-8"))
    allowed = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.NotIn)
                and ast.unparse(node.left) == "params.video_source"
                and "pexels" in ast.unparse(node.comparators[0])):
            allowed = eval(compile(ast.Expression(node.comparators[0]), "<check>", "eval"),
                           {"studio_service": studio})
    assert allowed is not None, "source validation not found in webui/Main.py"
    assert {"studio_scenes", "studio_story"} <= set(allowed)
    assert {"pexels", "local", "openai_image"} <= set(allowed)
