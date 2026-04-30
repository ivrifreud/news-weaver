"""Tests for agents/scout.py — all feedparser and HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agents.scout import (
    MAX_AGE_HOURS,
    MIN_SUMMARY_LENGTH,
    _fetch_feed,
    _fill_missing_summaries,
    _parse_date,
    _strip_html,
    fetch_all,
)
from models.schemas import RawArticle


def _make_entry(
    title: str,
    link: str,
    summary: str,
    published: Optional[str] = None,
    published_parsed=None,
) -> MagicMock:
    """Build a fake feedparser entry."""
    entry = MagicMock()
    entry.get = lambda key, default=None: {
        "title": title,
        "link": link,
        "summary": summary,
        "published": published,
        "published_parsed": published_parsed,
    }.get(key, default)
    return entry


def _make_feed(entries: list) -> MagicMock:
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = False
    feed.bozo_exception = None
    return feed


def _make_article(
    summary: str = "תקציר ארוך מספיק כדי לעבור את הסף",
    link: str = "https://example.com/1",
    source: str = "ynet",
) -> RawArticle:
    return RawArticle(
        title="כותרת",
        link=link,
        summary=summary,
        source=source,
        published_at=datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc),
    )


RECENT_RFC2822 = "Mon, 28 Apr 2026 10:00:00 +0000"
OLD_RFC2822 = "Mon, 28 Apr 2026 01:00:00 +0000"  # >6 h ago relative to 10:00 UTC


class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<p>Hello</p>") == "Hello"

    def test_decodes_entities(self):
        assert _strip_html("&amp;") == "&"
        assert _strip_html("&lt;b&gt;") == "<b>"

    def test_plain_text_unchanged(self):
        assert _strip_html("plain text") == "plain text"

    def test_mixed_html_and_text(self):
        result = _strip_html('<div><img alt="photo"> כותרת חדשותית </div>')
        assert "<" not in result
        assert "כותרת חדשותית" in result


class TestParseDate:
    def test_rfc2822_string(self):
        entry = _make_entry("t", "http://x", "s", published=RECENT_RFC2822)
        dt = _parse_date(entry)
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_iso_string_fallback(self):
        entry = _make_entry("t", "http://x", "s", published="2026-04-28T10:00:00Z")
        dt = _parse_date(entry)
        assert dt is not None
        assert dt.hour == 10

    def test_time_struct_fallback(self):
        import time
        ts = time.strptime("28 Apr 2026 10:00:00", "%d %b %Y %H:%M:%S")
        entry = _make_entry("t", "http://x", "s", published=None, published_parsed=ts)
        dt = _parse_date(entry)
        assert dt is not None

    def test_no_date_returns_none(self):
        entry = _make_entry("t", "http://x", "s", published=None, published_parsed=None)
        dt = _parse_date(entry)
        assert dt is None


class TestFetchFeed:
    def test_returns_raw_articles(self):
        entry = _make_entry("כותרת", "https://ynet.co.il/1", "תקציר", RECENT_RFC2822)
        feed = _make_feed([entry])
        cutoff = datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert len(articles) == 1
        assert isinstance(articles[0], RawArticle)
        assert articles[0].source == "ynet"
        assert articles[0].title == "כותרת"

    def test_strips_html_from_rss_summary(self):
        html_summary = '<div><img alt="photo"><p>תקציר חדשות חשוב מאוד</p></div>'
        entry = _make_entry("כותרת", "https://ynet.co.il/2", html_summary, RECENT_RFC2822)
        feed = _make_feed([entry])
        cutoff = datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert len(articles) == 1
        assert "<" not in articles[0].summary
        assert "תקציר חדשות חשוב מאוד" in articles[0].summary

    def test_filters_old_articles(self):
        old_entry = _make_entry("ישן", "https://ynet.co.il/old", "s", OLD_RFC2822)
        feed = _make_feed([old_entry])
        cutoff = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert articles == []

    def test_deduplicates_links(self):
        e1 = _make_entry("א", "https://x.com/1", "s", RECENT_RFC2822)
        e2 = _make_entry("ב", "https://x.com/1", "s", RECENT_RFC2822)
        feed = _make_feed([e1, e2])
        cutoff = datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert len(articles) == 1

    def test_skips_entry_with_no_date(self):
        entry = _make_entry("no date", "https://x.com/nd", "s", published=None, published_parsed=None)
        feed = _make_feed([entry])
        cutoff = datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert articles == []

    def test_handles_feedparser_exception(self):
        with patch("agents.scout.feedparser.parse", side_effect=Exception("network error")):
            articles = _fetch_feed("ynet", "https://dummy", datetime.now(timezone.utc), limit=100)

        assert articles == []

    def test_position_in_feed_is_set(self):
        # First two entries are old (skipped by cutoff); third entry is recent → position 3
        cutoff = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
        old = _make_entry("ישן", "https://x.com/old", "s", OLD_RFC2822)
        recent = _make_entry("חדש", "https://x.com/new", "תקציר ארוך מספיק", RECENT_RFC2822)
        feed = _make_feed([old, old, recent])  # recent is at feed position 3

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=100)

        assert len(articles) == 1
        assert articles[0].position_in_feed == 3

    def test_respects_per_feed_limit(self):
        entries = [
            _make_entry(f"כותרת {i}", f"https://x.com/{i}", "תקציר ארוך מספיק", RECENT_RFC2822)
            for i in range(10)
        ]
        feed = _make_feed(entries)
        cutoff = datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

        with patch("agents.scout.feedparser.parse", return_value=feed):
            articles = _fetch_feed("ynet", "https://dummy", cutoff, limit=3)

        assert len(articles) == 3
        # Positions should be 1, 2, 3 (first 3 entries in feed)
        assert [a.position_in_feed for a in articles] == [1, 2, 3]


class TestFillMissingSummaries:
    def test_short_summary_triggers_html_fallback(self):
        article = _make_article(summary="קצר")  # < MIN_SUMMARY_LENGTH
        long_content = "א" * 1000

        with patch("agents.scout._fetch_article_content", return_value=long_content):
            result = _fill_missing_summaries([article])

        assert len(result) == 1
        assert result[0].summary == long_content[:800]

    def test_adequate_summary_skips_html_fetch(self):
        article = _make_article(summary="תקציר ארוך מספיק שעובר את הסף של שלושים תווים בקלות")
        assert len(article.summary) >= MIN_SUMMARY_LENGTH

        with patch("agents.scout._fetch_article_content") as mock_fetch:
            _fill_missing_summaries([article])

        mock_fetch.assert_not_called()

    def test_fallback_returns_original_on_fetch_failure(self):
        article = _make_article(summary="קצר")

        with patch("agents.scout._fetch_article_content", return_value=None):
            result = _fill_missing_summaries([article])

        assert result[0].summary == "קצר"

    def test_preserves_article_order(self):
        articles = [
            _make_article(summary="א" * 50, link="https://example.com/1"),
            _make_article(summary="קצר", link="https://example.com/2"),
            _make_article(summary="ב" * 50, link="https://example.com/3"),
        ]

        with patch("agents.scout._fetch_article_content", return_value="מילוי" * 100):
            result = _fill_missing_summaries(articles)

        assert [a.link for a in result] == [a.link for a in articles]


class TestFetchAll:
    def test_aggregates_all_sources(self):
        def fake_parse(url: str):
            entry = _make_entry("כותרת", f"https://{url}/1", "תקציר ארוך מספיק", RECENT_RFC2822)
            return _make_feed([entry])

        with patch("agents.scout.feedparser.parse", side_effect=fake_parse):
            with patch("agents.scout._fetch_article_content", return_value=None):
                articles = fetch_all(max_age_hours=MAX_AGE_HOURS)

        assert len(articles) == 4

    def test_cross_feed_deduplication(self):
        shared_entry = _make_entry("dup", "https://shared.link/1", "תקציר ארוך מספיק", RECENT_RFC2822)
        feed = _make_feed([shared_entry])

        with patch("agents.scout.feedparser.parse", return_value=feed):
            with patch("agents.scout._fetch_article_content", return_value=None):
                articles = fetch_all(max_age_hours=MAX_AGE_HOURS)

        assert len(articles) == 1

    def test_returns_raw_article_instances(self):
        entry = _make_entry("כותרת", "https://ynet.co.il/99", "תקציר ארוך מספיק", RECENT_RFC2822)
        feed_with_article = _make_feed([entry])
        empty_feed = _make_feed([])
        feeds = [feed_with_article, empty_feed, empty_feed, empty_feed]

        with patch("agents.scout.feedparser.parse", side_effect=feeds):
            with patch("agents.scout._fetch_article_content", return_value=None):
                articles = fetch_all()

        assert all(isinstance(a, RawArticle) for a in articles)
