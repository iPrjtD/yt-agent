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
    assert relevance("cartoon broom", {"tags": "broom, cartoon"}) == 2  # asked for, so allowed


def test_full_description_lists_used_sources():
    script = ShortScript(title="t", hook="h", hook_visual_query="q", beats=[Beat(narration="n", visual_query="q", on_screen="o")] * 3,
                         outro="o", description="Why it matters.\n\nSources: [2]", tags=["a"], pillar="facts",
                         sources_used=[2])
    sources = [Source("A", "https://a.example", "x"), Source("B", "https://b.example", "y")]
    desc = full_description(script, sources)
    assert desc.startswith("Why it matters.")
    assert "- https://b.example" in desc and "a.example" not in desc
    assert desc.endswith("#shorts #buzzbee")
    with_credits = full_description(script, sources, ["Photo: A, CC BY 4.0, via Wikimedia Commons (u)"] * 2)
    assert with_credits.count("Photo: A, CC BY 4.0") == 1


def test_parse_dyk_extracts_fact_and_article():
    from ytagent.trends.ideas import parse_dyk

    html = ('<ul><li>... that <b><a href="/wiki/Octopus">octopuses</a></b> (pictured) have three hearts?</li>'
            '<li>Archive link</li></ul>')
    items = parse_dyk(html)
    assert len(items) == 1
    assert items[0].title == "Did you know: octopuses have three hearts"
    assert items[0].url == "https://en.wikipedia.org/wiki/Octopus"
