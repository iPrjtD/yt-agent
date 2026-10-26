from datetime import UTC

from ytagent.media.render import build_ass, caption_chunks
from ytagent.media.voice import Word, align_words
from ytagent.publish import full_description
from ytagent.research import Source
from ytagent.script import Beat, ShortScript


def test_align_words_uses_whisper_times_and_fills_gaps():
    lines = ["Why is everyone talking?", "Here is the buzz."]
    spans = [(0.0, 1.6), (1.8, 3.0)]
    heard = [("why", 0.0, 0.3), ("is", 0.3, 0.45), ("everyone", 0.45, 1.0), ("talking", 1.0, 1.5),
             ("here", 1.8, 2.0), ("buzz", 2.5, 2.9)]  # "is the" not heard
    words = align_words(lines, spans, heard)
    assert [w.text for w in words] == ["Why", "is", "everyone", "talking?", "Here", "is", "the", "buzz."]
    assert words[2].start == 0.45
    assert words[4].line == 1
    assert all(b.start >= a.start for a, b in zip(words, words[1:], strict=False))
    assert 1.8 <= words[6].start <= 3.0  # interpolated inside its line


def test_caption_chunks_break_on_lines_and_punctuation():
    words = [Word("Why", 0, 0.2, 0), Word("is", 0.2, 0.3, 0), Word("it", 0.3, 0.4, 0), Word("trending?", 0.4, 0.9, 0),
             Word("Because", 1.0, 1.3, 1), Word("reasons.", 1.3, 1.8, 1)]
    chunks = [[w.text for w in c] for c in caption_chunks(words)]
    assert chunks == [["Why", "is", "it"], ["trending?"], ["Because", "reasons."]]


def test_build_ass_highlights_current_word():
    words = [Word("Hello", 0.0, 0.5, 0), Word("buzz", 0.5, 1.0, 0)]
    ass = build_ass(words, [(0.0, 1.0, "big news")], 1080, 1920)
    assert "Style: Caption,Poppins ExtraBold" in ass
    assert ",Label,,0,0,0,,BIG NEWS" in ass
    assert "{\\c&H0000C3FF&}HELLO{\\c&H00FFFFFF&} BUZZ" in ass
    assert "HELLO {\\c&H0000C3FF&}BUZZ" in ass


def test_clip_relevance_uses_tag_overlap():
    from ytagent.media.visuals import relevance

    smog = {"tags": "smog, city, pollution, haze"}
    dog = {"tags": "dog, pet, animal, looking"}
    assert relevance("person looking at smoggy sky", dog) == 0
    assert relevance("smoggy city skyline", smog) >= 1
    assert relevance("cities in smog", smog) == 1  # plural "cities" does not match "city"; "smog" does
    assert relevance("street sweeper broom", {"tags": "broom, sweeper, cartoon, woman"}) == 0
    assert relevance("cartoon broom", {"tags": "broom, cartoon"}) == 0  # the channel look is real footage only


def test_full_description_lists_used_sources():
    script = ShortScript(title="t", hook="h", hook_visual_query="q", beats=[Beat(narration="n", visual_query="q", on_screen="o", subject="")] * 3,
                         outro="o", description="Why it matters.\n\nSources: [2]", tags=["a"], pillar="facts",
                         sources_used=[2])
    sources = [Source("A", "https://a.example", "x"), Source("B", "https://b.example", "y")]
    desc = full_description(script, sources)
    assert desc.startswith("Why it matters.")
    assert "- https://b.example" in desc and "a.example" not in desc
    assert desc.endswith("#shorts #buzzbee")
    with_credits = full_description(script, sources, ["Photo: A, CC BY 4.0, via Wikimedia Commons (u)"] * 2)
    assert with_credits.count("Photo: A, CC BY 4.0") == 1


def test_fill_subjects_marks_story_beats_up_to_the_quote():
    from ytagent.pipeline import _fill_subjects

    beats = [Beat(narration=n, visual_query="q", on_screen="o", subject="") for n in
             ["Born poor.", "Became a star.", 'He said "I remain a clown."', "Your struggles do not define you."]]
    script = ShortScript(title="t", hook="h", hook_visual_query="q", beats=beats, outro="o", description="d",
                         tags=["a"], pillar="motivation")
    _fill_subjects(script, "Charlie Chaplin")
    assert [b.subject for b in script.beats] == ["Charlie Chaplin"] * 3 + [""]


def test_spoke_extra_rejects_takes_that_read_the_direction():
    from ytagent.media.voice import spoke_extra

    assert not spoke_extra(22, 22)
    assert not spoke_extra(22, 25)
    assert spoke_extra(22, 80)  # the 3.8 model read the whole instruction paragraph aloud


def test_shot_times_cut_where_the_phrase_is_spoken():
    from ytagent.pipeline import _shot_times
    from ytagent.script import Shot

    words = [Word(t, i * 0.5, i * 0.5 + 0.4, 0) for i, t in enumerate(
        "Before age nine he was sent to a workhouse twice and by fourteen his mother was in an asylum".split())]
    shots = [Shot(words="Before age nine", query="q", scene="s", subject=""), Shot(words="and by fourteen", query="q", scene="s", subject="")]
    cuts = _shot_times(shots, words, start=0.0, end=10.0)
    assert cuts == [0.0, 5.0]  # "and" is word 10 -> 5.0 s
    unknown = [Shot(words="Before age", query="q", scene="s", subject=""), Shot(words="not in line", query="q", scene="s", subject="")]
    assert _shot_times(unknown, words, 0.0, 10.0) == [0.0, 5.0]  # not found: evenly spread
    gap = [Shot(words="Before age", query="q", scene="s", subject=""), Shot(words="nowhere", query="q", scene="s", subject=""),
           Shot(words="his mother", query="q", scene="s", subject="")]
    assert _shot_times(gap, words, 0.0, 10.0) == [0.0, 3.25, 6.5]  # a missing middle cue no longer crashes


def test_historic_shots_are_recognised():
    from ytagent.pipeline import HISTORIC

    assert HISTORIC.search("ancient indian surgeon reconstructing a nose")
    assert HISTORIC.search("a boy on a london street in the 1890s")
    assert HISTORIC.search("Mughal emperor in his court")
    assert not HISTORIC.search("modern surgeon in an operating room")
    assert not HISTORIC.search("crowd laughing in a theatre")


def test_next_slot_respects_lead_time_and_gap():
    from datetime import datetime

    from ytagent.review import next_slot

    tz = "Asia/Kolkata"  # UTC+5:30
    now = datetime(2026, 10, 25, 7, 0, tzinfo=UTC)  # 12:30 local
    first = next_slot(now, ["13:00", "19:30"], [], tz, 4)
    assert first == datetime(2026, 10, 25, 7, 30, tzinfo=UTC)  # 13:00 local, 30 min ahead
    second = next_slot(now, ["13:00", "19:30"], [first], tz, 4)
    assert second == datetime(2026, 10, 25, 14, 0, tzinfo=UTC)  # 19:30 local
    late = datetime(2026, 10, 25, 7, 20, tzinfo=UTC)  # 12:50 local: 13:00 is too close
    assert next_slot(late, ["13:00", "19:30"], [], tz, 4) == second


def test_stale_lock_is_taken_over_when_owner_process_is_gone(tmp_path, monkeypatch):
    from ytagent import autopilot

    monkeypatch.setattr(autopilot, "get_settings", lambda: type("S", (), {"data_dir": tmp_path})())
    (tmp_path / "autopilot.lock").write_text("999999999", encoding="utf-8")  # no such process
    with autopilot.lock():
        assert (tmp_path / "autopilot.lock").read_text(encoding="utf-8") == str(__import__("os").getpid())
    assert not (tmp_path / "autopilot.lock").exists()


def test_parse_dyk_extracts_fact_and_article():
    from ytagent.trends.ideas import parse_dyk

    html = ('<ul><li>... that <b><a href="/wiki/Octopus">octopuses</a></b> (pictured) have three hearts?</li>'
            '<li>Archive link</li></ul>')
    items = parse_dyk(html)
    assert len(items) == 1
    assert items[0].title == "Did you know: octopuses have three hearts"
    assert items[0].url == "https://en.wikipedia.org/wiki/Octopus"


def test_key_terms_keep_the_distinctive_part_of_a_name():
    from ytagent.media.visuals import key_terms

    assert key_terms("Mount Roraima") == {"roraima"}
    assert key_terms("saffron crocus") == {"saffron", "crocu"}  # plural-stripped, like the tags it is matched to
    assert key_terms("Taj Mahal") == {"taj", "mahal"}
    assert key_terms("Great Wall") == {"wall"}
    assert key_terms("Mountain") == {"mountain"}  # nothing distinctive: keep what there is


def test_main_subject_is_the_name_most_shots_use():
    from ytagent.pipeline import main_subject
    from ytagent.script import BeatPlan, MemePlan, Shot

    def step(*subjects):
        return BeatPlan(emotion="warm", reaction="none", sfx="none", meme_top="", meme_bottom="", buzz_line="",
                        shots=[Shot(words="w", query="q", scene="s", subject=x) for x in subjects])

    plan = MemePlan.model_construct(hook=step("saffron crocus", ""), beats=[step("", "saffron crocus"), step("Kashmir")])
    assert main_subject(plan) == "saffron crocus"
    assert main_subject(MemePlan.model_construct(hook=step("Kashmir"), beats=[step("")])) == ""


def test_lock_held_by_a_live_job_is_waited_for_then_refused(tmp_path, monkeypatch):
    import os
    import time

    import pytest

    from ytagent import autopilot

    monkeypatch.setattr(autopilot, "get_settings", lambda: type("S", (), {"data_dir": tmp_path})())
    (tmp_path / "autopilot.lock").write_text(str(os.getppid()), encoding="utf-8")  # a live process
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="another autopilot job"), autopilot.lock(wait_s=0):
        pass
