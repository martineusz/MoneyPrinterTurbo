"""Title card rendered with Pillow. Original design: a rounded panel with an avatar, channel
name, optional label pill and the title. It intentionally has no platform UI, logos, vote
counts or award icons."""

import os

from PIL import Image, ImageDraw, ImageFont

DEFAULTS = {
    "background_color": "#14161c", "opacity": 0.92, "radius": 36,
    "channel_name": "Channel", "label": "",
    "title_size": 64, "title_color": "#ffffff", "channel_size": 38, "channel_color": "#c9ced8",
    "label_color": "#ffffff", "label_background": "#3b82f6",
}
MIN_TITLE_SIZE = 30
MAX_TITLE_CHARS = 220


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _font_candidates(preferred: str | None) -> list[str]:
    from app.utils import utils

    fonts = utils.font_dir()
    names = ["BeVietnamPro-Bold.ttf", "MicrosoftYaHeiBold.ttc", "STHeitiMedium.ttc", "Charm-Bold.ttf"]
    return ([preferred] if preferred else []) + [os.path.join(fonts, n) for n in names]


def pick_font_file(text: str, preferred: str | None = None) -> str | None:
    """First font that has glyphs for ``text`` (so non-ASCII titles do not render as boxes)."""
    candidates = [c for c in _font_candidates(preferred) if c and os.path.isfile(c)]
    try:
        from app.services import video

        for path in candidates:
            if video.subtitle_font_supports_text(path, text):
                return path
    except Exception:  # glyph probing is best effort
        pass
    return candidates[0] if candidates else None


def _load(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    tokens = text.split(" ")
    if len(tokens) < 3 and len(text) > 12:  # unspaced (e.g. CJK) text wraps by character
        tokens = list(text)
        joiner = ""
    else:
        joiner = " "
    lines, current = [], ""
    for token in tokens:
        trial = token if not current else current + joiner + token
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = token
    if current:
        lines.append(current)
    return lines


def _fit_title(draw, title: str, font_path, size: int, max_width: int, max_height: int):
    """Shrink the font until the wrapped title fits; ellipsise as a last resort."""
    title = " ".join(title.split())
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    while True:
        font = _load(font_path, size)
        lines = _wrap(draw, title, font, max_width)
        line_h = int(size * 1.25)
        if len(lines) * line_h <= max_height or size <= MIN_TITLE_SIZE:
            break
        size -= 4
    max_lines = max(1, max_height // line_h)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[:-1].rstrip() + "…"
    return font, lines, line_h


def render(path: str, width: int, height: int, title: str, settings: dict | None = None,
           label_extra: str = "") -> str:
    """Write a transparent PNG of size (width, height) with the card centered. Returns path."""
    s = {**DEFAULTS, **(settings or {})}
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    card_w = int(width * 0.88)
    pad = int(width * 0.05)
    all_text = f"{title} {s['channel_name']} {s['label']} {label_extra}"
    font_path = pick_font_file(all_text, s.get("font"))

    header_h = int(width * 0.13)
    inner_w = card_w - 2 * pad
    title_font, lines, line_h = _fit_title(
        draw, title, font_path, int(s["title_size"]), inner_w, int(height * 0.28))
    card_h = pad + header_h + pad // 2 + len(lines) * line_h + pad
    left, top = (width - card_w) // 2, int(height * 0.20)

    alpha = int(max(0.0, min(1.0, float(s["opacity"]))) * 255)
    draw.rounded_rectangle(
        (left, top, left + card_w, top + card_h), radius=int(s["radius"]),
        fill=(*_rgb(s["background_color"]), alpha))

    x, y = left + pad, top + pad
    avatar_size = header_h
    avatar = s.get("avatar")
    if avatar and os.path.isfile(avatar):
        with Image.open(avatar) as source:
            tile = source.convert("RGBA").resize((avatar_size, avatar_size))
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
        image.paste(tile, (x, y), mask)
    else:
        draw.ellipse((x, y, x + avatar_size, y + avatar_size), fill=(*_rgb(s["label_background"]), 255))
        initial = (s["channel_name"] or "?").strip()[:1].upper()
        initial_font = _load(font_path, int(avatar_size * 0.55))
        draw.text((x + avatar_size / 2, y + avatar_size / 2), initial, font=initial_font,
                  fill=(255, 255, 255, 255), anchor="mm")

    name_font = _load(font_path, int(s["channel_size"]))
    text_x = x + avatar_size + pad // 2
    draw.text((text_x, y + avatar_size / 2), s["channel_name"], font=name_font,
              fill=(*_rgb(s["channel_color"]), 255), anchor="lm")

    label = " · ".join(p for p in (s["label"], label_extra) if p)
    if label:
        label_font = _load(font_path, max(18, int(s["channel_size"] * 0.7)))
        tw = draw.textlength(label, font=label_font)
        pill_w, pill_h = int(tw + 36), int(label_font.size * 1.9)
        px, py = left + card_w - pad - pill_w, y + (avatar_size - pill_h) // 2
        draw.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=pill_h // 2,
                               fill=(*_rgb(s["label_background"]), 255))
        draw.text((px + pill_w / 2, py + pill_h / 2), label, font=label_font,
                  fill=(*_rgb(s["label_color"]), 255), anchor="mm")

    ty = y + header_h + pad // 2
    for line in lines:
        draw.text((x, ty), line, font=title_font, fill=(*_rgb(s["title_color"]), 255))
        ty += line_h

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    image.save(path, "PNG")
    return path
