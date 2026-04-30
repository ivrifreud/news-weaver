"""Telegram Bot API wrapper — async only (python-telegram-bot v21)."""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from models.schemas import ProcessedEvent

logger = logging.getLogger(__name__)


def _format_message(event: ProcessedEvent) -> str:
    """Format a ProcessedEvent as a Telegram HTML message."""
    source_names = ", ".join({a.source for a in event.sources})
    score_bar = "⭐" * round(event.relevance_score)
    return (
        f"<b>📰 חדשות</b>\n\n"
        f"{event.combined_summary}\n\n"
        f"<i>מקורות: {source_names}</i>\n"
        f"ציון רלוונטיות: {score_bar} ({event.relevance_score:.1f})"
    )


def _make_keyboard(event_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with Like / Dislike / Expand buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 אהבתי", callback_data=f"like:{event_id}"),
            InlineKeyboardButton("👎 לא רלוונטי", callback_data=f"dislike:{event_id}"),
        ],
        [
            InlineKeyboardButton("🔍 הרחב", callback_data=f"expand:{event_id}"),
        ],
    ])


async def send_notification(
    bot: Bot,
    chat_id: str,
    event: ProcessedEvent,
) -> Optional[int]:
    """Send a push notification for an event. Returns message_id on success."""
    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=_format_message(event),
            parse_mode="HTML",
            reply_markup=_make_keyboard(event.event_id),
        )
        logger.info("Sent notification for event %s (msg_id=%s)", event.event_id, message.message_id)
        return message.message_id
    except Exception as exc:
        logger.error("Failed to send Telegram notification: %s", exc)
        return None
