"""Command-line options for studio, kept import-light so ``cli.py --help`` stays fast."""

import argparse


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "studio", "post-processing, scene and story modes (see docs/STUDIO.md); "
                  "use --video-source studio_scenes or studio_story for the two modes")
    group.add_argument("--studio-preset", default=None, metavar="NAME",
                       help="preset folder under [studio] presets_dir (default: [studio] default_preset)")
    group.add_argument("--studio-post", action="store_true", default=None,
                       help="post-process final videos with the preset's post.json")
    group.add_argument("--studio-story-title", default=None, metavar="TEXT",
                       help="story mode title card text (default: first sentence of the script)")
    group.add_argument("--studio-footage-dir", default=None, metavar="DIR",
                       help="story mode folder of background videos")
    group.add_argument("--studio-split-parts", action="store_true", default=None,
                       help="story mode: split narration longer than [studio] max_part_seconds "
                            "into parts (requires --studio-post)")


_FIELDS = {
    "studio_preset": "studio_preset",
    "studio_post": "studio_post",
    "studio_story_title": "studio_story_title",
    "studio_footage_dir": "studio_footage_dir",
    "studio_split_parts": "studio_story_split_parts",
}


def params_from_args(args: argparse.Namespace) -> dict:
    """VideoParams keyword arguments for the studio options that were given."""
    return {
        field: getattr(args, option)
        for option, field in _FIELDS.items()
        if getattr(args, option, None) is not None
    }
