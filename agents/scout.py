"""Node 1 — The Scout: fetch and parse RSS feeds into RawArticle objects."""

from __future__ import annotations

import html as _html
import logging
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Set

import feedparser
import trafilatura
from dateutil import parser as dateutil_parser

from models.schemas import RawArticle

try:
    import certifi
    # macOS Python 3.8 ships without root certs; point to certifi's bundle
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10      # seconds per article HTTP request
ENRICH_WORKERS = 8      # parallel threads for summary fallback fetches
MIN_SUMMARY_LENGTH = 30  # chars of real text required to skip HTML fetch

FEEDS: Dict[str, str] = {
    # Israeli ideological balance
    "ynet":            "https://www.ynet.co.il/Integration/StoryRss2.xml",
    "israel_hayom":    "https://www.israelhayom.co.il/rss.xml",
    # Global strategic depth
    "bbc_world":       "https://feeds.bbci.co.uk/news/world/rss.xml",
    "the_economist":   "https://www.economist.com/latest/rss.xml",
    "foreign_affairs": "https://www.foreignaffairs.com/rss.xml",
    # Professional tech & economy
    "the_marker":      "https://www.themarker.com/srv/tm-all-articles",
    "geektime":        "https://www.geektime.co.il/feed/",
    "mit_tech_review": "https://www.technologyreview.com/feed/",
}

# Per-source article quotas (tune per tier as needed)
FEED_LIMITS: Dict[str, int] = {
    "ynet":            5,
    "israel_hayom":    5,
    "bbc_world":       5,
    "the_economist":    3,
    "foreign_affairs":  2,
    "the_marker":       4,
    "geektime":         3,
    "mit_tech_review":  2,
}

MAX_AGE_HOURS = 24


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from a string."""
    clean = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(clean).strip()


def _parse_date(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    """Extract a timezone-aware datetime from a feed entry."""
    for attr in ("published", "updated"):
        raw = entry.get(attr)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        try:
            dt = dateutil_parser.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    for time_struct_attr in ("published_parsed", "updated_parsed"):
        ts = entry.get(time_struct_attr)
        if ts:
            try:
                dt = datetime(*ts[:6], tzinfo=timezone.utc)
                return dt
            except Exception:
                pass

    return None


def _fetch_feed(source: str, url: str, cutoff: datetime, limit: int) -> List[RawArticle]:
    """Fetch a single RSS feed and return up to `limit` recent articles."""
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        logger.error("Failed to fetch feed %s (%s): %s", source, url, exc)
        return []

    if feed.bozo and feed.bozo_exception:
        logger.warning("Feed %s parsed with errors: %s", source, feed.bozo_exception)

    articles: List[RawArticle] = []
    seen_links: Set[str] = set()
    feed_pos: int = 0  # 1-based position across all feed entries (including skipped ones)

    for entry in feed.entries:
        feed_pos += 1  # increment for every entry, even ones we skip

        link: str = entry.get("link", "").strip()
        if not link or link in seen_links:
            continue
        seen_links.add(link)

        published_at = _parse_date(entry)
        if published_at is None:
            logger.debug("Skipping entry with no date from %s: %s", source, link)
            continue
        if published_at < cutoff:
            continue

        title: str = entry.get("title", "").strip()
        summary_raw: str = entry.get("summary", entry.get("description", "")).strip()
        summary: str = _strip_html(summary_raw)

        try:
            article = RawArticle(
                title=title,
                link=link,
                summary=summary,
                source=source,
                published_at=published_at,
                position_in_feed=feed_pos,
            )
            articles.append(article)
        except Exception as exc:
            logger.warning("Skipping malformed entry from %s: %s", source, exc)

        if len(articles) >= limit:
            break

    logger.info("Fetched %d/%d articles from %s", len(articles), limit, source)
    return articles


def _fetch_article_content(url: str) -> Optional[str]:
    """Download a single article URL and extract its main text via trafilatura."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsWeaver/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")
        return trafilatura.extract(raw_html, include_comments=False, include_tables=False)
    except Exception as exc:
        logger.debug("Content fetch failed for %s: %s", url, exc)
        return None


def _fill_missing_summaries(articles: List[RawArticle]) -> List[RawArticle]:
    """For articles whose RSS summary is too short, fetch and fill from HTML."""
    needs_fill = [a for a in articles if len(a.summary) < MIN_SUMMARY_LENGTH]
    if not needs_fill:
        return articles

    logger.info("%d articles need summary fallback from HTML", len(needs_fill))

    def _fetch_summary(article: RawArticle) -> RawArticle:
        content = _fetch_article_content(article.link)
        if content:
            return article.model_copy(update={"summary": content[:800]})
        return article

    filled: Dict[str, RawArticle] = {}
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as executor:
        futures: Dict = {executor.submit(_fetch_summary, a): a for a in needs_fill}
        for future in as_completed(futures):
            try:
                result = future.result()
                filled[result.link] = result
            except Exception as exc:
                original = futures[future]
                logger.warning("Summary fallback failed for %s: %s", original.link, exc)
                filled[original.link] = original

    filled_count = sum(
        1 for a in filled.values() if len(a.summary) >= MIN_SUMMARY_LENGTH
    )
    logger.info("Filled summaries for %d/%d articles via HTML", filled_count, len(needs_fill))
    return [filled.get(a.link, a) for a in articles]


def fetch_all(max_age_hours: int = MAX_AGE_HOURS) -> List[RawArticle]:
    """Fetch all configured RSS feeds and return deduplicated recent articles.

    Summaries are always clean text: RSS excerpt when sufficient,
    HTML-extracted fallback otherwise.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    all_articles: List[RawArticle] = []
    seen_links: Set[str] = set()

    for source, url in FEEDS.items():
        limit = FEED_LIMITS.get(source, 25)
        for article in _fetch_feed(source, url, cutoff, limit):
            if article.link not in seen_links:
                seen_links.add(article.link)
                all_articles.append(article)

    logger.info("Total unique articles fetched: %d", len(all_articles))
    all_articles = _fill_missing_summaries(all_articles)
    return all_articles


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    articles = fetch_all()
    print(f"Fetched {len(articles)} articles in the last {MAX_AGE_HOURS} hours.")
    for a in articles[:5]:
        print(f"  [{a.source}] {a.title[:80]}")
