import json
import os
import random

import pytest
from PIL import Image

from app.models.schema import VideoParams
from app.services import studio
from app.services.studio import captions, ffmpeg_util, footage, post, presets, story, titlecard
from app.services.studio.captions import Word

from .studio_helpers import ff, make_preset, make_task, use_presets_dir


# ---- footage picker -------------------------------------------------------------

def test_choose_never_overlaps_used_ranges_until_exhausted():
    durations = {"a.mp4": 100.0, "b.mp4": 50.0}
    used: dict = {}
    rng = random.Random(7)
    picked = []
    for _ in range(8):  # well below the 15-slot capacity, so no reuse is needed
        name, start, reused = footage.choose(durations, used, 10.0, rng)
        assert not reused
        for a, b in used.get(name, []):
            assert start >= b - 1e-6 or start + 10.0 <= a + 1e-6, (name, start, (a, b))
        used.setdefault(name, []).append([start, start + 10.0])
        picked.append((name, start))
    assert len(set(picked)) == 8


def test_choose_reports_exhaustion_and_allows_reuse():
    durations = {"a.mp4": 20.0}
    used = {"a.mp4": [[0.0, 20.0]]}
    name, start, reused = footage.choose(durations, used, 10.0, random.Random(1))
    assert reused and name == "a.mp4" and 0 <= start <= 10


def test_choose_rejects_footage_that_is_too_short():
    with pytest.raises(footage.FootageError, match="long enough"):
        footage.choose({"a.mp4": 5.0}, {}, 10.0)


def test_free_intervals():
    assert footage.free_intervals(100, [[10, 20], [15, 30], [50, 60]]) == [(0, 10), (30, 50), (60, 100)]
    assert footage.free_intervals(10, []) == [(0, 10)]


def make_footage(folder, names=("one.mp4", "two.mp4"), seconds=30):
    os.makedirs(folder, exist_ok=True)
    for i, name in enumerate(names):
        ff("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds}", "-f", "lavfi", "-i",
           f"sine=frequency={300 + 100 * i}:duration={seconds}", "-shortest", "-pix_fmt", "yuv420p",
           os.path.join(folder, name))
    return folder


def test_pick_segment_logs_ranges_and_resets_when_exhausted(tmp_path, caplog):
    folder = make_footage(str(tmp_path / "fg"), names=("one.mp4",), seconds=20)
    rng = random.Random(3)
    first = footage.pick_segment(folder, 12.0, rng)
    log = json.load(open(os.path.join(folder, footage.LOG_NAME)))
    assert log["one.mp4"] == [[first[1], first[1] + 12.0]]
    second = footage.pick_segment(folder, 12.0, rng)  # 20s file cannot hold two 12s windows
    log = json.load(open(os.path.join(folder, footage.LOG_NAME)))
    assert log["one.mp4"] == [[second[1], second[1] + 12.0]]


def test_pick_segment_errors(tmp_path):
    with pytest.raises(footage.FootageError, match="not found"):
        footage.pick_segment(str(tmp_path / "missing"), 5)
    (tmp_path / "empty").mkdir()
    with pytest.raises(footage.FootageError, match="no video files"):
        footage.pick_segment(str(tmp_path / "empty"), 5)


# ---- chunking + alignment --------------------------------------------------------

def words(text, step=0.4):
    return [Word(t, round(i * step, 3), round(i * step + step, 3)) for i, t in enumerate(text.split())]


def test_chunk_words_limits_and_sentence_boundaries():
    chunks = captions.chunk_words(words("One two three four five. Six seven"), max_words=3)
    assert [[w.text for w in c] for c in chunks] == [
        ["One", "two", "three"], ["four", "five."], ["Six", "seven"]]
    assert captions.chunk_words([], 3) == []
    single = captions.chunk_words(words("a b c"), max_words=1)
    assert [len(c) for c in single] == [1, 1, 1]


def test_chunk_words_respects_character_budget():
    chunks = captions.chunk_words(words("extraordinary magnificent unbelievable"), max_words=3, max_chars=24)
    assert all(len(c) <= 2 for c in chunks) and sum(len(c) for c in chunks) == 3


def test_align_corrects_misrecognised_words_and_fills_missing_ones():
    script = "The quick brown fox jumps over the lazy dog".split()
    heard = [Word("the", 0.0, 0.2), Word("quik", 0.2, 0.6), Word("brown", 0.6, 1.0),
             Word("fox", 1.0, 1.4),
             Word("over", 2.0, 2.3), Word("the", 2.3, 2.5), Word("lazy", 2.5, 3.0), Word("dog", 3.0, 3.4)]
    out = captions.align(script, heard, 3.4)
    assert [w.text for w in out] == script  # script text wins over "quik"
    assert (out[1].start, out[1].end) == (0.2, 0.6)  # replaced word keeps the heard window
    assert (out[4].start, out[4].end) == (1.4, 2.0)  # "jumps" was missed: fills the gap
    assert all(a.end <= b.start + 1e-9 for a, b in zip(out, out[1:]))
    assert out[-1].end <= 3.4 + 1e-9


def test_align_without_any_recognition_spreads_over_audio():
    out = captions.align(["alpha", "be", "gamma"], [], 6.0)
    assert out[0].start == 0 and abs(out[-1].end - 6.0) < 0.01
    assert out[0].end - out[0].start > out[1].end - out[1].start  # longer word gets more time


def test_align_handles_punctuation_case_and_unicode():
    script = ["Cześć,", "świecie!", "Dzień"]
    heard = [Word("cześć", 0, 0.5), Word("swiecie", 0.5, 1.0), Word("dzień", 1.0, 1.5)]
    out = captions.align(script, heard, 1.5)
    assert [w.text for w in out] == script and out[0].end == 0.5
    assert captions.align([], heard, 1.0) == []


def test_caption_ass_highlights_current_word_per_event():
    text = captions.build_caption_ass(words("hello big world"), 1080, 1920,
                                      {"color": "#FFFFFF", "highlight_color": "#FF0000", "uppercase": False})
    events = [line for line in text.splitlines() if line.startswith("Dialogue")]
    assert len(events) == 3
    assert events[0].endswith("{\\c&H0000FF&}hello{\\c&HFFFFFF&} big world")
    assert events[2].endswith("hello big {\\c&H0000FF&}world{\\c&HFFFFFF&}")
    assert "\\an" not in text and ",5,60,60," in text  # middle alignment


def test_caption_ass_shifts_and_clips_to_a_part_window():
    text = captions.build_caption_ass(words("a b c d e f", step=1.0), 100, 100, {}, offset=2.0, end_limit=3.0)
    events = [line for line in text.splitlines() if line.startswith("Dialogue")]
    assert events and events[0].split(",")[1] == "0:00:00.00"
    assert all(e.split(",")[2] <= "0:00:03.00" for e in events)


def test_lines_to_words_covers_each_line():
    from app.services.studio.srt import parse_srt
    out = captions.lines_to_words(parse_srt("1\n00:00:01,000 --> 00:00:03,000\nhello wide world\n"))
    assert out[0].start == 1.0 and abs(out[-1].end - 3.0) < 0.01 and len(out) == 3


# ---- parts / title -----------------------------------------------------------------

def test_first_sentence():
    assert story.first_sentence("It began. Then more.") == "It began."
    assert story.first_sentence("") == ""
    assert story.first_sentence("x" * 300).endswith("…") and len(story.first_sentence("x" * 300)) == 140


def test_plan_parts_cuts_on_sentence_ends_and_tiles():
    ws = words("a b c d. e f g h. i j k l. m n", step=1.0)  # 14 seconds
    parts = story.plan_parts(ws, 14.0, 6.0)
    assert parts[0][0] == 0 and parts[-1][1] == 14.0
    assert all(a[1] == b[0] for a, b in zip(parts, parts[1:]))
    assert all(end - start <= 6.0 + 1e-6 for start, end in parts[:-1])
    assert parts[0][1] == 4.0  # cut after the sentence end "d."


def test_plan_parts_no_split_and_no_sentence_ends():
    ws = words("a b c", step=1.0)
    assert story.plan_parts(ws, 3.0, 10.0) == [(0.0, 3.0)]
    assert story.plan_parts(ws, 3.0, 0) == [(0.0, 3.0)]
    long = story.plan_parts(words("w " * 20, step=1.0), 20.0, 6.0)
    assert all(end - start <= 6.0 + 1e-6 for start, end in long[:-1]) and long[-1][1] == 20.0


# ---- title card --------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Short title", "A very long title " * 30, "Zażółć gęślą jaźń: 日本語のタイトル 🚀", "", "x" * 500,
])
def test_title_card_renders_at_video_size(tmp_path, title):
    out = titlecard.render(str(tmp_path / "card.png"), 540, 960, title,
                           {"channel_name": "Kanał ✓", "label": "story time"}, label_extra="Part 2")
    with Image.open(out) as image:
        assert image.size == (540, 960) and image.mode == "RGBA"
        assert image.getchannel("A").getbbox() is not None


def test_title_card_with_avatar_and_custom_colors(tmp_path):
    avatar = tmp_path / "a.png"
    Image.new("RGB", (50, 80), (255, 0, 0)).save(avatar)
    out = titlecard.render(str(tmp_path / "c.png"), 400, 700, "Hi", {
        "avatar": str(avatar), "background_color": "#ffffff", "opacity": 1.0, "radius": 0})
    with Image.open(out) as image:
        assert image.getpixel((200, int(700 * 0.2) + 10))[3] == 255


# ---- end to end ------------------------------------------------------------------

SRT = ("1\n00:00:00,000 --> 00:00:03,000\nThis is the story. It was strange\n\n"
       "2\n00:00:03,000 --> 00:00:06,000\nand then it ended. Really\n")
SCRIPT = "This is the story. It was strange and then it ended. Really"


def fake_transcribe(audio, model, device, language):
    out, t = [], 0.0
    for text in "this is the storey it was strange and then it ended really".split():
        out.append(Word(text, t, t + 0.45))
        t += 0.5
    return out


@pytest.fixture
def story_env(tmp_path, monkeypatch):
    footage_dir = make_footage(str(tmp_path / "footage"), seconds=40)
    use_presets_dir(monkeypatch, tmp_path / "presets", footage_dir=footage_dir)
    make_preset(tmp_path / "presets", "st", {"look": {"grain": 3}},
                style={"story": {"title_card": {"channel_name": "Tester", "label": "story time"},
                                 "captions": {"size": 80, "highlight_color": "#FFD400"}}})
    folder = make_task(str(tmp_path / "task"), video_seconds=2, audio_seconds=6, srt=SRT, size="180x320")
    with open(os.path.join(folder, "script.json"), "w", encoding="utf-8") as handle:
        json.dump({"script": SCRIPT, "params": {}}, handle)
    from app.utils import utils
    monkeypatch.setattr(utils, "task_dir", lambda sub="": folder)
    return folder, footage_dir


def test_story_end_to_end_without_post_burns_captions(story_env):
    folder, _ = story_env
    params = VideoParams(video_subject="x", video_source="studio_story", studio_preset="st")
    paths = story.build_for_task("t", params, 6.0, transcribe=fake_transcribe, rng=random.Random(1))
    assert paths == [os.path.join(folder, "studio-combined-1.mp4")]
    info = ffmpeg_util.probe(paths[0])
    assert (info.width, info.height) == (1080, 1920) and abs(info.duration - 6.1) < 0.15
    assert os.path.isfile(os.path.join(folder, "captions.ass"))
    ass_text = open(os.path.join(folder, "captions.ass"), encoding="utf-8").read()
    assert "STORY" in ass_text.upper() and "storey" not in ass_text  # script text, not the ASR typo
    # MPT must not burn its own subtitles over studio's captions
    seen = {}
    render = studio.render_override(params, lambda **kw: seen.update(kw))
    render(video_path="v", audio_path="a", subtitle_path=os.path.join(folder, "subtitle.srt"),
           output_file="o", params=params)
    assert seen["subtitle_path"] == ""


def test_story_falls_back_to_line_captions_without_faster_whisper(story_env, monkeypatch):
    folder, _ = story_env
    monkeypatch.setattr(captions, "whisper_available", lambda: False)
    params = VideoParams(video_subject="x", video_source="studio_story", studio_preset="st",
                         studio_story_title="Custom ünïcode title")
    story.build_for_task("t", params, 6.0, rng=random.Random(2))
    assert os.path.isfile(os.path.join(folder, "captions.ass"))


def test_story_title_falls_back_to_first_sentence(story_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(titlecard, "render", lambda path, w, h, title, *a, **k: seen.update(title=title) or path)
    params = VideoParams(video_subject="x", video_source="studio_story", studio_preset="st")
    with pytest.raises(Exception):  # the fake card file does not exist; only the title matters here
        story.build_for_task("t", params, 6.0, transcribe=fake_transcribe, rng=random.Random(1))
    assert seen["title"] == "This is the story."


def test_story_with_post_hands_captions_to_post_and_keeps_visuals_clean(story_env, tmp_path):
    folder, _ = story_env
    params = VideoParams(video_subject="x", video_source="studio_story", studio_preset="st",
                         studio_post=True)
    paths = story.build_for_task("t", params, 6.0, transcribe=fake_transcribe, rng=random.Random(1))
    combined = os.path.join(folder, "combined-1.mp4")
    studio.combine_override(params, object())(combined_video_path=combined, video_paths=paths)
    # post.json 'subtitles' must be ignored in favour of captions.ass
    preset = presets.load("st")
    plan = post.build_plan(folder, preset, write_files=False)
    assert any("story captions in use" in n for n in plan.notes) or not preset.post.get("subtitles")
    graph = plan.cmd[plan.cmd.index("-filter_complex") + 1]
    assert "captions.ass" in graph
    out = post.apply_to_final(folder, preset)
    assert abs(ffmpeg_util.probe(out).duration - 6.0) < 0.2


def test_story_split_parts_end_to_end(story_env, monkeypatch, tmp_path):
    folder, footage_dir = story_env
    use_presets_dir(monkeypatch, tmp_path / "presets", footage_dir=footage_dir, max_part_seconds=3.5)
    params = VideoParams(video_subject="x", video_source="studio_story", studio_preset="st",
                         studio_post=True, studio_story_split_parts=True)
    paths = story.build_for_task("t", params, 6.0, transcribe=fake_transcribe, rng=random.Random(5))
    assert len(paths) >= 2 and params.video_count == len(paths)  # cut at sentence ends
    assert os.path.isfile(os.path.join(folder, "audio-part2.mp3"))
    preset = presets.load("st")
    total = 0.0
    for n, path in enumerate(paths, 1):
        combined = os.path.join(folder, f"combined-{n}.mp4")
        studio.combine_override(params, object())(combined_video_path=combined, video_paths=paths)
        final = os.path.join(folder, f"final-{n}.mp4")
        # MPT's own render is bypassed for parts: the mux uses the part audio
        studio.render_override(params, object())(
            video_path=combined, audio_path=os.path.join(folder, "audio.mp3"), subtitle_path="",
            output_file=final, params=params)
        out = post.apply_to_final(folder, preset, n)
        total += ffmpeg_util.probe(out).duration
    assert abs(total - 6.0) < 0.5


def test_preflight_story_rules(tmp_path, monkeypatch):
    use_presets_dir(monkeypatch, tmp_path / "presets")
    make_preset(tmp_path / "presets", "st", {"look": {}}, style={})
    base = dict(video_subject="x", video_source="studio_story", studio_preset="st")
    assert "footage folder" in studio.preflight(VideoParams(**base), "video")
    fg = make_footage(str(tmp_path / "fg"), names=("a.mp4",), seconds=2)
    assert studio.preflight(VideoParams(**base, studio_footage_dir=fg), "video") == ""
    problem = studio.preflight(VideoParams(**base, studio_footage_dir=fg, studio_story_split_parts=True), "video")
    assert "split_parts needs studio_post" in problem
    assert studio.preflight(
        VideoParams(**base, studio_footage_dir=fg, studio_story_split_parts=True, studio_post=True), "video") == ""
