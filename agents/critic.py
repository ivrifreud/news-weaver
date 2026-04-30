"""Node 2 — The Critic: cluster → synthesize+score → return ProcessedEvents."""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from models.schemas import ProcessedEvent, RawArticle, UserProfile
from services.llm_client import HAIKU_MODEL, LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences (```json...``` or ```...```) from LLM output."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_thinking(text: str) -> Tuple[str, str]:
    """Extract <thinking>...</thinking> block. Returns (thinking, remainder)."""
    match = re.search(r"<thinking>(.*?)</thinking>", text, re.DOTALL)
    if match:
        thinking = match.group(1).strip()
        remainder = (text[: match.start()] + text[match.end() :]).strip()
        return thinking, remainder
    return "", text.strip()


def _extract_score_block(text: str) -> Tuple[str, str]:
    """Extract <score>...</score> block. Returns (score_json, remainder)."""
    match = re.search(r"<score>(.*?)</score>", text, re.DOTALL)
    if match:
        score_json = match.group(1).strip()
        remainder = (text[: match.start()] + text[match.end() :]).strip()
        return score_json, remainder
    return "", text.strip()


def _compute_prominence(sources: List[RawArticle]) -> dict:
    """Compute editorial prominence signals from article feed positions."""
    top_headline_sources = [a.source for a in sources if 0 < a.position_in_feed <= 3]
    return {
        "top_headline_sources": top_headline_sources,
        "source_count": len(sources),
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_cluster_prompt(articles: List[RawArticle]) -> str:
    """Prompt 1 — group headlines by real-world event (runs on Haiku)."""
    articles_data = [
        {"index": i, "title": a.title, "source": a.source,
         "position_in_feed": a.position_in_feed}
        for i, a in enumerate(articles)
    ]
    articles_json = json.dumps(articles_data, ensure_ascii=False, indent=2)
    return (
        "<task>\n"
        "קבץ את הכותרות הבאות לפי האירוע האמיתי שהן מתארות.\n"
        "כל כתבה שייכת לקבוצה אחת בדיוק. החזר JSON תקין בלבד.\n"
        "</task>\n"
        f"<articles>\n{articles_json}\n</articles>\n"
        "<output_format>\n"
        '{"clusters": [{"event_id": "<uuid>", "article_indices": [0, 2, 5]}, ...]}\n'
        "</output_format>"
    )


def _build_synthesis_and_score_prompt(
    articles: List[RawArticle], profile: UserProfile
) -> str:
    """Prompt 2 — synthesise Hebrew summary AND score relevance in one call."""
    # position_in_feed omitted from article rows — already captured in <prominence>
    articles_data = [
        {"title": a.title, "source": a.source, "summary": a.summary}
        for a in articles
    ]
    articles_json = json.dumps(articles_data, ensure_ascii=False, indent=2)

    profile_data = {
        "interests": [{"topic": i.topic, "weight": i.weight} for i in profile.interests],
        "avoid_topics": profile.avoid_topics,
    }
    profile_json = json.dumps(profile_data, ensure_ascii=False)
    prominence_json = json.dumps(_compute_prominence(articles), ensure_ascii=False)

    return (
        "<task>\n"
        "אתה עורך חדשות. הכתבות עשויות להיות בעברית או באנגלית — הסיכום חייב להיות בעברית. בצע שלושה שלבים:\n"
        "1. נתח את הכתבות בתגיות <thinking> (זהה את האירוע, זוויות שונות, חשיבות עיתונאית).\n"
        "2. כתוב סיכום של 3-4 משפטים בעברית — כשynet ו-israel_hayom מכסים את אותו אירוע, ציין במפורש את הבדלי הפריימינג. ציין מקורות כותרת ראשית (top_headline_sources) כשרלוונטי.\n"
        "3. דרג רלוונטיות (1.0-10.0) בהתאם לפרופיל המשתמש ולחשיבות העיתונאית.\n"
        "פורמט חובה (אל תסטה ממנו):\n"
        "<thinking>[הניתוח שלך]</thinking>\n"
        "[הסיכום בעברית]\n"
        '<score>{"relevance_score": <float>, "reasoning": "<הסבר קצר>"}</score>\n'
        "</task>\n"
        f"<articles>\n{articles_json}\n</articles>\n"
        f"<prominence>\n{prominence_json}\n</prominence>\n"
        f"<user_profile>\n{profile_json}\n</user_profile>"
    )


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def _cluster_articles(
    articles: List[RawArticle], client: LLMClient
) -> List[List[RawArticle]]:
    """Call Haiku to group articles into clusters. Falls back to singletons on error."""
    if not articles:
        return []

    titles_preview = ", ".join(a.title[:30] for a in articles[:5])
    logger.info("[Critic/cluster] Grouping %d articles — %s…", len(articles), titles_preview)
    prompt = _build_cluster_prompt(articles)
    raw = ""
    try:
        raw = client.call(prompt, max_tokens=2000, model=HAIKU_MODEL)
        data = json.loads(_strip_fences(raw))
        clusters = data.get("clusters", [])

        assigned: set = set()
        groups: List[List[RawArticle]] = []

        for cluster in clusters:
            indices = cluster.get("article_indices", [])
            group: List[RawArticle] = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(articles) and idx not in assigned:
                    group.append(articles[idx])
                    assigned.add(idx)
            if group:
                groups.append(group)

        for i, article in enumerate(articles):
            if i not in assigned:
                groups.append([article])

        logger.info("Clustered %d articles into %d events", len(articles), len(groups))
        return groups

    except Exception as exc:
        logger.warning("Clustering failed (%s) — raw response: %r", exc, raw)
        return [[a] for a in articles]


def _synthesize_and_score_cluster(
    articles: List[RawArticle], profile: UserProfile, client: LLMClient
) -> Tuple[str, str, float, str]:
    """Single LLM call: synthesise + score. Returns (summary, thinking, score, score_reasoning)."""
    titles_preview = " | ".join(a.title[:30] for a in articles)
    logger.info("[Critic/synthesize+score] %d-article cluster: %s", len(articles), titles_preview)
    prompt = _build_synthesis_and_score_prompt(articles, profile)
    raw = ""
    try:
        raw = client.call(prompt, max_tokens=2500)

        # Extract CoT thinking
        thinking, after_thinking = _extract_thinking(raw)

        # Extract score JSON block
        score_json_str, summary = _extract_score_block(after_thinking)

        # Parse score
        score = 5.0
        score_reasoning = ""
        if score_json_str:
            try:
                score_data = json.loads(_strip_fences(score_json_str))
                score = float(score_data.get("relevance_score", 5.0))
                score = max(1.0, min(10.0, score))
                score_reasoning = str(score_data.get("reasoning", ""))
            except Exception as parse_exc:
                # Regex fallback: Hebrew text can contain unescaped " (e.g. כטב"ם) breaking json.loads
                m = re.search(r'"relevance_score"\s*:\s*(\d+(?:\.\d+)?)', score_json_str)
                if m:
                    score = max(1.0, min(10.0, float(m.group(1))))
                logger.warning(
                    "Score JSON parse failed (%s) — raw score block: %r — regex score: %.1f",
                    parse_exc, score_json_str, score,
                )

        if not summary:
            logger.warning("No summary extracted for cluster of %d articles", len(articles))
            summary = articles[0].summary if articles else ""

        return summary, thinking, score, score_reasoning

    except Exception as exc:
        logger.error("Synthesize+score failed: %s — raw: %r", exc, raw)
        return (articles[0].summary if articles else ""), "", 5.0, ""


# ---------------------------------------------------------------------------
# Kept for backward compatibility with existing tests
# ---------------------------------------------------------------------------

def _synthesize_cluster(
    articles: List[RawArticle], profile: UserProfile, client: LLMClient
) -> Tuple[str, str]:
    """Synthesise a Hebrew summary for a cluster. Returns (combined_summary, reasoning)."""
    from services.llm_client import MODEL
    titles_preview = " | ".join(a.title[:30] for a in articles)
    logger.info("[Critic/synthesize] Synthesising %d-article cluster: %s", len(articles), titles_preview)
    prompt = _build_synthesis_prompt(articles, profile)
    raw = ""
    try:
        raw = client.call(prompt, max_tokens=2000)
        reasoning, summary = _extract_thinking(raw)
        if not summary:
            logger.warning(
                "Synthesis returned no summary for cluster of %d articles", len(articles)
            )
        return summary, reasoning
    except Exception as exc:
        logger.error("Synthesis failed: %s", exc)
        return (articles[0].summary if articles else ""), ""


def _build_synthesis_prompt(articles: List[RawArticle], profile: UserProfile) -> str:
    """Legacy Prompt 2 — kept for test compatibility."""
    articles_data = [
        {"title": a.title, "source": a.source, "summary": a.summary,
         "position_in_feed": a.position_in_feed}
        for a in articles
    ]
    articles_json = json.dumps(articles_data, ensure_ascii=False, indent=2)
    interests_json = json.dumps(
        [{"topic": i.topic, "weight": i.weight} for i in profile.interests],
        ensure_ascii=False,
    )
    return (
        "<task>\n"
        "אתה עורך חדשות בעברית. ראשית נתח את הכתבות בתגיות <thinking>, "
        "לאחר מכן כתוב סיכום של 3-4 משפטים בעברית.\n"
        "אם המקורות מציגים זוויות שונות, ציין זאת במפורש.\n"
        "שים לב: position_in_feed מציין את מיקום הכתבה בעמוד הבית של האתר — "
        "כתבות במיקום 1-3 הן כותרות ראשיות; ציין זאת בסיכום כשרלוונטי.\n"
        "פורמט חובה:\n"
        "<thinking>\n[הניתוח שלך כאן]\n</thinking>\n"
        "[הסיכום בעברית כאן]\n"
        "</task>\n"
        f"<articles>\n{articles_json}\n</articles>\n"
        f"<user_interests>\n{interests_json}\n</user_interests>"
    )


def _build_scoring_prompt(
    summary: str, profile: UserProfile, sources: List[RawArticle]
) -> str:
    """Legacy Prompt 3 — kept for test compatibility."""
    profile_data = {
        "interests": [{"topic": i.topic, "weight": i.weight} for i in profile.interests],
        "avoid_topics": profile.avoid_topics,
    }
    profile_json = json.dumps(profile_data, ensure_ascii=False)

    min_pos = min((a.position_in_feed for a in sources if a.position_in_feed > 0), default=0)
    top_headline_sources = [a.source for a in sources if 0 < a.position_in_feed <= 3]
    prominence_data = {
        "min_position_in_feed": min_pos,
        "top_headline_sources": top_headline_sources,
        "source_count": len(sources),
    }
    prominence_json = json.dumps(prominence_data, ensure_ascii=False)

    return (
        "<task>\n"
        "דרג את הרלוונטיות של האירוע הבא לתחומי העניין של המשתמש בסולם 1.0 עד 10.0.\n"
        "קח בחשבון גם את החשיבות העיתונאית: אם האירוע הופיע ככותרת ראשית (position 1-3) "
        "במספר אתרים, ייתכן שמדובר בחדשה חשובה גם אם אינה תואמת לחלוטין לתחומי העניין.\n"
        "החזר JSON תקין בלבד, ללא גדרות markdown:\n"
        '{"relevance_score": <float 1.0-10.0>, "reasoning": "<הסבר קצר בעברית>"}\n'
        "</task>\n"
        f"<event_summary>\n{summary}\n</event_summary>\n"
        f"<editorial_prominence>\n{prominence_json}\n</editorial_prominence>\n"
        f"<user_profile>\n{profile_json}\n</user_profile>"
    )


def _score_event(
    summary: str,
    profile: UserProfile,
    client: LLMClient,
    sources: Optional[List[RawArticle]] = None,
) -> Tuple[float, str]:
    """Legacy scorer — kept for test compatibility."""
    logger.info("[Critic/score] Scoring event — summary: %.60s…", summary)
    prompt = _build_scoring_prompt(summary, profile, sources or [])
    raw = ""
    try:
        raw = client.call(prompt, max_tokens=500)
        data = json.loads(_strip_fences(raw))
        score = float(data.get("relevance_score", 5.0))
        score = max(1.0, min(10.0, score))
        reasoning = str(data.get("reasoning", ""))
        return score, reasoning
    except Exception as exc:
        logger.warning("Scoring failed (%s) — raw response: %r", exc, raw)
        return 5.0, ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def process_articles(
    articles: List[RawArticle], profile: UserProfile, client: LLMClient
) -> List[ProcessedEvent]:
    """Cluster, then synthesise+score each cluster — parallel Sonnet calls, 5 workers."""
    if not articles:
        return []

    groups = _cluster_articles(articles, client)

    def _process_group(group: List[RawArticle]) -> Optional[ProcessedEvent]:
        summary, thinking, score, score_reasoning = _synthesize_and_score_cluster(
            group, profile, client
        )
        reasoning_parts = [p for p in (thinking, score_reasoning) if p]
        full_reasoning = (
            "\n\nציון: ".join(reasoning_parts)
            if len(reasoning_parts) > 1
            else (reasoning_parts[0] if reasoning_parts else "")
        )
        try:
            return ProcessedEvent(
                event_id=str(uuid.uuid4()),
                combined_summary=summary,
                relevance_score=score,
                reasoning=full_reasoning,
                sources=group,
            )
        except Exception as exc:
            logger.error("Failed to build ProcessedEvent: %s", exc)
            return None

    events: List[ProcessedEvent] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_process_group, g): g for g in groups}
        for future in as_completed(futures):
            try:
                event = future.result()
                if event is not None:
                    events.append(event)
            except Exception as exc:
                logger.error("Synthesize+score thread failed: %s", exc)

    logger.info(
        "Produced %d processed events from %d articles", len(events), len(articles)
    )
    return events
