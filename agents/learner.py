"""Node 4 — The Learner: handle Telegram callbacks, update UserProfile."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from filelock import FileLock
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, ContextTypes

from models.schemas import UserProfile
from services.llm_client import LLMClient

logger = logging.getLogger(__name__)

_PROFILE_PATH = "data/user_profile.json"
_EVENTS_CACHE_PATH = "data/events_cache.json"
_FEEDBACK_QUEUE_PATH = "data/feedback_queue.json"
_LOCK_PATH = _PROFILE_PATH + ".lock"


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

def _load_profile(path: str = _PROFILE_PATH) -> UserProfile:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return UserProfile.model_validate(json.load(f))
    return UserProfile()


def _save_profile(profile: UserProfile, path: str = _PROFILE_PATH) -> None:
    """Atomic write: FileLock + temp file + os.replace()."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_events_cache(path: str = _EVENTS_CACHE_PATH) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Feedback queue  (persisted between bot restarts / pipeline runs)
# ---------------------------------------------------------------------------

def _enqueue_feedback(event_id: str, action: str) -> None:
    """Append a feedback item to the persistent queue (atomic write)."""
    queue: List[dict] = []
    if os.path.exists(_FEEDBACK_QUEUE_PATH):
        try:
            with open(_FEEDBACK_QUEUE_PATH, encoding="utf-8") as f:
                queue = json.load(f)
        except Exception:
            queue = []

    queue.append({
        "event_id": event_id,
        "action": action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    tmp = _FEEDBACK_QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _FEEDBACK_QUEUE_PATH)
    logger.debug("Enqueued %s for event %s", action, event_id)


def process_feedback_queue(
    profile_path: str = _PROFILE_PATH,
    cache_path: str = _EVENTS_CACHE_PATH,
    queue_path: str = _FEEDBACK_QUEUE_PATH,
) -> int:
    """Drain the feedback queue, apply weight adjustments, return items processed."""
    if not os.path.exists(queue_path):
        return 0

    try:
        with open(queue_path, encoding="utf-8") as f:
            queue: List[dict] = json.load(f)
    except Exception as exc:
        logger.warning("Could not read feedback queue: %s", exc)
        return 0

    if not queue:
        return 0

    events_cache = _load_events_cache(cache_path)

    with FileLock(_LOCK_PATH):
        profile = _load_profile(profile_path)

        for item in queue:
            event_id = item.get("event_id", "")
            action = item.get("action", "")
            if not event_id or action not in ("like", "dislike"):
                continue

            is_positive = action == "like"
            delta = 0.1 if is_positive else -0.1

            signals = (profile.behavior_memory.positive_signals
                       if is_positive else profile.behavior_memory.negative_signals)
            if event_id not in signals:
                signals.append(event_id)

            event_data = events_cache.get(event_id, {})
            keywords = (event_data.get("summary", "")
                        if isinstance(event_data, dict) else str(event_data))
            if keywords:
                for interest in profile.interests:
                    if interest.topic in keywords:
                        interest.weight = max(0.0, min(1.0, interest.weight + delta))
                        logger.info(
                            "Queue: adjusted '%s' by %+.1f → %.2f",
                            interest.topic, delta, interest.weight,
                        )

        _save_profile(profile, profile_path)

    # Clear the queue atomically
    tmp = queue_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([], f)
    os.replace(tmp, queue_path)

    logger.info("Processed %d queued feedback items", len(queue))
    return len(queue)


# ---------------------------------------------------------------------------
# Expand helpers
# ---------------------------------------------------------------------------

def _build_expand_prompt(articles_content: List[Dict]) -> str:
    """Build prompt for an 8-10 sentence deep-dive summary."""
    content_parts = []
    for item in articles_content:
        content_parts.append(
            f"[{item['source']}] {item['title']}\n{item['content'][:2000]}"
        )
    combined = "\n\n---\n\n".join(content_parts)
    return (
        "<task>\n"
        "אתה עורך חדשות מעמיק. כתוב סיכום מפורט של 8-10 משפטים בעברית על האירוע הבא.\n"
        "כלול: רקע, פרטים מרכזיים, זוויות שונות של המקורות, והשלכות אפשריות.\n"
        "הכתבות עשויות להיות בעברית או באנגלית — הסיכום חייב להיות בעברית.\n"
        "</task>\n"
        f"<sources>\n{combined}\n</sources>"
    )


async def _fetch_content_async(url: str) -> Optional[str]:
    """Fetch article content in an async context (runs sync fetch in executor)."""
    import asyncio
    from agents.scout import _fetch_article_content
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_article_content, url)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process Like / Dislike — writes to queue (survives system restarts)."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    data = query.data
    if ":" not in data:
        logger.warning("Unexpected callback_data: %s", data)
        return

    action, event_id = data.split(":", 1)
    if action not in ("like", "dislike"):
        logger.warning("Unknown action: %s", action)
        return

    try:
        _enqueue_feedback(event_id, action)
    except Exception as exc:
        logger.error("Failed to enqueue feedback: %s", exc)

    response = "👍 תודה על המשוב!" if action == "like" else "👎 הבנתי, לא רלוונטי."
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(response)
    logger.info("Enqueued %s for event %s", action, event_id)


async def handle_expand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch full article content and reply with an 8-10 sentence deep summary."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer("מביא תוכן מלא… ⏳")

    event_id = query.data.split(":", 1)[1]
    cache = _load_events_cache()
    event_data = cache.get(event_id, {})
    sources = event_data.get("sources", []) if isinstance(event_data, dict) else []

    if not sources:
        await query.message.reply_text("⚠️ לא נמצאו מקורות לאירוע זה.")
        return

    # Fetch full content for each source article
    articles_content = []
    for src in sources:
        content = await _fetch_content_async(src["link"])
        if content and len(content) > 100:
            articles_content.append({
                "source": src.get("source", ""),
                "title": src.get("title", ""),
                "content": content,
            })
        else:
            # Fall back to cached summary snippet
            snippet = event_data.get("summary", "")
            if snippet:
                articles_content.append({
                    "source": src.get("source", ""),
                    "title": src.get("title", ""),
                    "content": snippet,
                })

    if not articles_content:
        await query.message.reply_text("⚠️ לא הצלחתי לטעון תוכן מלא מהמקורות.")
        return

    try:
        client = LLMClient()
        prompt = _build_expand_prompt(articles_content)
        expanded = client.call(prompt, max_tokens=1500)
    except Exception as exc:
        logger.error("Expand LLM call failed: %s", exc)
        await query.message.reply_text("⚠️ שגיאה בהכנת הסיכום המורחב.")
        return

    await query.message.reply_text(f"📖 *סיכום מורחב*\n\n{expanded}", parse_mode="Markdown")
    await query.edit_message_reply_markup(reply_markup=None)

    # Auto-like: expanding signals strong interest
    try:
        _enqueue_feedback(event_id, "like")
    except Exception as exc:
        logger.warning("Could not enqueue auto-like after expand: %s", exc)

    logger.info("Expand handled for event %s (%d sources fetched)", event_id, len(articles_content))


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

def build_application(token: str) -> Application:
    """Build and configure the PTB Application (does not start it)."""
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CallbackQueryHandler(handle_feedback, pattern=r"^(like|dislike):"))
    app.add_handler(CallbackQueryHandler(handle_expand, pattern=r"^expand:"))
    return app
