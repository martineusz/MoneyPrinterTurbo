"""Per-scene camera motion: zoompan expressions and motion selection."""

import random

MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static")
FPS = 30
OVERSAMPLE = 1.5
ZOOM = 0.15  # total zoom travel for zoom_* motions; pans use a fixed 1.15 crop


def pick_motions(count: int, allowed: list[str], order: str, seeds: list[int]) -> list[str]:
    bad = [m for m in allowed if m not in MOTIONS]
    if bad or not allowed:
        raise ValueError(f"scene_motions must be a non-empty subset of {', '.join(MOTIONS)}; got {allowed}")
    if order == "cycle":
        return [allowed[i % len(allowed)] for i in range(count)]
    return [random.Random(seeds[i]).choice(allowed) for i in range(count)]


def zoompan_filter(motion: str, frames: int, width: int, height: int) -> str:
    """Return the video filter chain that turns one still image into ``frames`` frames.

    Expressions avoid commas so no extra escaping is needed. ``on`` is the output frame
    number; progress is on/frames.
    """
    if motion not in MOTIONS:
        raise ValueError(f"unknown motion {motion!r}")
    n = max(1, frames)
    p = f"(on/{n})"
    centre_x, centre_y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    z, x, y = "1", "0", "0"
    if motion == "zoom_in":
        z, x, y = f"1+{ZOOM}*{p}", centre_x, centre_y
    elif motion == "zoom_out":
        z, x, y = f"1+{ZOOM}-{ZOOM}*{p}", centre_x, centre_y
    elif motion == "pan_left":
        z, x, y = "1.15", f"(iw-iw/zoom)*(1-{p})", centre_y
    elif motion == "pan_right":
        z, x, y = "1.15", f"(iw-iw/zoom)*{p}", centre_y
    elif motion == "pan_up":
        z, x, y = "1.15", centre_x, f"(ih-ih/zoom)*(1-{p})"
    elif motion == "pan_down":
        z, x, y = "1.15", centre_x, f"(ih-ih/zoom)*{p}"
    big_w, big_h = int(width * OVERSAMPLE) // 2 * 2, int(height * OVERSAMPLE) // 2 * 2
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={width}x{height}:fps={FPS},"
        f"setsar=1,format=yuv420p"
    )
