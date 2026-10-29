"""ASS subtitle generation (styles, escaping, colors)."""

import re

from .srt import Cue

_ALIGNMENT = {"bottom": 2, "middle": 5, "top": 8}


def ass_color(color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS stores colors as BGR)."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color or "")
    if not m:
        raise ValueError(f"invalid color {color!r}; expected #RRGGBB")
    rr, gg, bb = m[1][0:2], m[1][2:4], m[1][4:6]
    return f"&H{alpha:02X}{bb}{gg}{rr}".upper()


def override_color(color: str) -> str:
    """Color for an inline ``\\c`` override tag: '&HBBGGRR&'."""
    return "&H" + ass_color(color)[4:] + "&"


def ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def escape_text(text: str, uppercase: bool = False) -> str:
    if uppercase:
        text = text.upper()
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\n", "\\N")


def header(width: int, height: int, style: dict, name: str = "Default") -> str:
    align = _ALIGNMENT[style.get("position", "bottom")]
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {name},{style.get('font', 'Arial')},{style.get('size', 64)},"
        f"{ass_color(style.get('color', '#FFFFFF'))},{ass_color(style.get('color', '#FFFFFF'))},"
        f"{ass_color(style.get('outline_color', '#000000'))},&H80000000,"
        f"{-1 if style.get('bold', True) else 0},0,0,0,100,100,{style.get('letter_spacing', 0)},0,1,"
        f"{style.get('outline_width', 3)},{style.get('shadow', 0)},{align},60,60,"
        f"{style.get('margin_v', 120)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def build_ass(cues: list[Cue], width: int, height: int, style: dict) -> str:
    upper = bool(style.get("uppercase", False))
    lines = [header(width, height, style)]
    for cue in cues:
        lines.append(
            f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Default,,0,0,0,,"
            f"{escape_text(cue.text, upper)}\n"
        )
    return "".join(lines)
