"""Shared picture geometry so scene rendering and post-processing agree on the 4:3 box."""


def frame43_box(width: int, height: int) -> tuple[int, int] | None:
    """(top, box_height) of the centered 4:3 landscape picture inside a ``width`` x ``height``
    frame (look.frame_43), or None when the frame is not taller than a 4:3 picture.

    Both scenes.py (render the image so it fills this box) and post.py (keep this box, black
    bars elsewhere) call this, so the crop never discards any of the scene image.
    """
    box_h = int(width * 3 / 4) // 2 * 2
    if box_h >= height:
        return None
    return (height - box_h) // 2 // 2 * 2, box_h
