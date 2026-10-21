from ytagent.trends.base import log_strength, rank_strength, topic_key
from ytagent.trends.sources import (
    _parse_traffic,
    parse_google_news,
    parse_google_trends,
    parse_hackernews,
    parse_wikipedia_top,
    parse_youtube_popular,
)

GOOGLE_TRENDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
<channel><title>Daily Search Trends</title>
<item>
  <title>india vs australia</title>
  <ht:approx_traffic>50K+</ht:approx_traffic>
  <pubDate>Wed, 21 Oct 2026 13:40:00 -0700</pubDate>
  <ht:news_item>
    <ht:news_item_title>India beat Australia &amp; level the series</ht:news_item_title>
    <ht:news_item_url>https://example.com/a</ht:news_item_url>
  </ht:news_item>
  <ht:news_item>
    <ht:news_item_title>Five takeaways from the match</ht:news_item_title>
    <ht:news_item_url>https://example.com/b</ht:news_item_url>
  </ht:news_item>
</item>
<item>
  <title>small topic</title>
  <ht:approx_traffic>100+</ht:approx_traffic>
</item>
</channel></rss>"""

GOOGLE_NEWS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Top stories</title>
<item><title>Big launch announced today - Example Times</title><link>https://news.example/1</link>
<guid>g1</guid><pubDate>Wed, 21 Oct 2026 14:52:48 GMT</pubDate>
<description>&lt;ol&gt;&lt;li&gt;&lt;a href="x"&gt;Big launch announced today&lt;/a&gt;&lt;/li&gt;&lt;li&gt;&lt;a href="y"&gt;Launch: what it means&lt;/a&gt;&lt;/li&gt;&lt;/ol&gt;</description>
</item>
<item><title>Second story - Other Paper</title><link>https://news.example/2</link><guid>g2</guid></item>
</channel></rss>"""


def test_parse_traffic():
    assert _parse_traffic("2,000+") == 2000
    assert _parse_traffic("50K+") == 50_000
    assert _parse_traffic("1M+") == 1_000_000
    assert _parse_traffic("") == 0


def test_google_trends_keeps_all_news_items():
    items = parse_google_trends(GOOGLE_TRENDS_XML)
    assert [i.title for i in items] == ["india vs australia", "small topic"]
    top = items[0]
    assert top.related == 2
    assert "India beat Australia & level the series" in top.context
    assert top.url == "https://example.com/a"
    assert top.raw["traffic"] == 50_000
    assert top.strength > items[1].strength
    assert top.published_at is not None and top.published_at.tzinfo is not None


def test_google_news_strips_publisher_and_counts_coverage():
    items = parse_google_news(GOOGLE_NEWS_XML)
    assert items[0].title == "Big launch announced today"
    assert items[0].related == 2
    assert items[0].context == "Launch: what it means"
    assert items[0].strength > items[1].strength


def test_wikipedia_skips_non_articles():
    data = {"items": [{"articles": [
        {"article": "Main_Page", "views": 6_000_000, "rank": 1},
        {"article": "Special:Search", "views": 1_000_000, "rank": 2},
        {"article": "Kim_Jong_Un", "views": 321_739, "rank": 3},
        {"article": "Jailer_2", "views": 303_129, "rank": 4},
    ]}]}
    items = parse_wikipedia_top(data, "en.wikipedia", top_n=1)
    assert [i.title for i in items] == ["Kim Jong Un"]
    assert items[0].url == "https://en.wikipedia.org/wiki/Kim_Jong_Un"


def test_hackernews_min_points():
    data = {"hits": [
        {"objectID": "1", "title": "Big", "points": 900, "num_comments": 400, "created_at_i": 1792590333},
        {"objectID": "2", "title": "Small", "points": 20, "num_comments": 1, "created_at_i": 1792590333},
    ]}
    items = parse_hackernews(data, min_points=150)
    assert [i.title for i in items] == ["Big"]
    assert items[0].url == "https://news.ycombinator.com/item?id=1"


def test_youtube_popular():
    data = {"items": [{"id": "abc", "snippet": {"title": "T", "channelTitle": "C", "tags": ["x"],
                                                 "publishedAt": "2026-10-21T10:00:00Z"},
                       "statistics": {"viewCount": "1000000"}}]}
    item = parse_youtube_popular(data)[0]
    assert item.url == "https://www.youtube.com/watch?v=abc"
    assert 0 < item.strength < 1


def test_topic_key_is_order_insensitive():
    assert topic_key("Messi joins Inter Miami") == topic_key("Inter Miami: Messi joins!")
    assert topic_key("The iPhone 18 launch") == topic_key("iphone 18 launch")


def test_strength_helpers():
    assert log_strength(0, 100) == 0
    assert log_strength(10_000_000, 1_000_000) == 1.0
    assert rank_strength(0, 10) == 1.0
    assert rank_strength(9, 10) == 0.2
