"""Collapsible "Studio" section for the WebUI (see docs/STUDIO.md).

Strings are plain English passed through MPT's ``tr`` so a translation can be added later;
untranslated keys fall back to the English text itself.
"""

import os

import streamlit as st
from loguru import logger

SOURCE_LABELS = {
    "studio_scenes": "Studio: scene images",
    "studio_story": "Studio: story over footage",
}
# (group label, sources) pairs for the Video Source selector
SOURCE_GROUPS = (("Studio", ("studio_scenes", "studio_story")),)


def _recent_scene_tasks(limit: int = 15) -> list[str]:
    from app.utils import utils

    root = utils.task_dir()
    found = []
    for name in os.listdir(root):
        marker = os.path.join(root, name, "scenes.json")
        if os.path.isfile(marker):
            found.append((os.path.getmtime(marker), name))
    return [name for _, name in sorted(found, reverse=True)[:limit]]


def _render_scene_editor(tr):
    from app.services.studio import motion, scenes
    from app.utils import utils

    st.markdown(f"**{tr('Scenes')}**")
    tasks = _recent_scene_tasks()
    if not tasks:
        st.caption(tr("No task with a scenes.json yet. Generate a video with the scene source first."))
        return
    task_id = st.selectbox(tr("Task"), tasks, key="studio_scene_task")
    folder = os.path.join(utils.task_dir(), task_id)
    try:
        doc = scenes.load_doc(folder)
    except scenes.SceneError as exc:
        st.error(str(exc))
        return
    rows = [{k: s[k] for k in ("index", "start", "end", "narration", "prompt", "seed", "motion")}
            for s in doc["scenes"]]
    edited = st.data_editor(
        rows, key=f"studio_scene_editor_{task_id}", hide_index=True, use_container_width=True,
        disabled=["index", "start", "end", "narration"],
        column_config={"motion": st.column_config.SelectboxColumn(options=list(motion.MOTIONS))},
    )
    picked = st.multiselect(tr("Scenes to regenerate"), [r["index"] for r in rows],
                            key="studio_scene_pick")
    cols = st.columns(3)
    final = cols[2].checkbox(tr("Also re-run the final render"), value=True, key="studio_scene_final")

    def apply_edits():
        for original, row in zip(doc["scenes"], edited):
            original["prompt"], original["seed"], original["motion"] = (
                row["prompt"], int(row["seed"]), row["motion"])
        scenes.validate_doc(doc)
        scenes.save_doc(folder, doc)

    if cols[0].button(tr("Save edits"), key="studio_scene_save"):
        try:
            apply_edits()
            st.success(tr("Saved scenes.json"))
        except (scenes.SceneError, ValueError, TypeError) as exc:
            st.error(str(exc))
    if cols[1].button(tr("Regenerate selected scenes"), key="studio_scene_regen"):
        try:
            apply_edits()
            with st.spinner(tr("Rendering scenes...")):
                out = scenes.rerender(folder, set(picked), final=final)
            st.success(f"{tr('Done')}: {out}")
        except Exception as exc:  # surface any backend/ffmpeg error in the UI
            logger.exception("studio: scene re-render failed")
            st.error(str(exc))


def render(params, tr=lambda key: key):
    """Render the Studio section and write the studio_* fields onto ``params``."""
    from app.services.studio import config as studio_config
    from app.services.studio import presets

    with st.expander(tr("Studio"), expanded=False):
        try:
            cfg = studio_config.get()
        except studio_config.StudioConfigError as exc:
            st.error(str(exc))
            return
        names = presets.list_presets(cfg)
        if not names:
            st.info(tr("No presets found. Create a folder under the presets directory "
                       "(see presets/example and docs/STUDIO.md)."))
            return
        default = cfg["default_preset"] if cfg["default_preset"] in names else names[0]
        name = st.selectbox(tr("Preset"), names, index=names.index(default), key="studio_preset_select")
        params.studio_preset = name
        preset = None
        try:
            preset = presets.load(name, cfg)
        except presets.PresetError as exc:
            for problem in exc.problems:
                st.error(problem)

        has_post = bool(preset and preset.post is not None)
        params.studio_post = st.checkbox(
            tr("Post-process the final video (look, styled subtitles, BGM, cues)"),
            value=False, disabled=not has_post, key="studio_post_checkbox",
            help=tr("Needs post.json in the preset. Replaces MPT's own subtitle burn-in and BGM."),
        ) and has_post

        if params.video_source == "studio_scenes":
            st.caption(tr("Scene mode: one generated image per narration segment. "
                          "Image backend: ") + cfg["image_backend"])
        elif params.video_source == "studio_story":
            params.studio_story_title = st.text_input(
                tr("Story title (empty = first sentence of the script)"), key="studio_story_title")
            params.studio_footage_dir = st.text_input(
                tr("Footage folder"), value=cfg["footage_dir"], key="studio_footage_dir_input")
            params.studio_story_split_parts = st.checkbox(
                tr("Split long narration into parts"), value=False, key="studio_split_parts",
                help=tr("Uses [studio] max_part_seconds and requires post-processing."))
        else:
            st.caption(tr("Select a Studio video source to use scene or story mode."))
        st.divider()
        _render_scene_editor(tr)
