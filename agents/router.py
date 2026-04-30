"""Node 3 — The Router: dynamic thresholding + daily log."""

from __future__ import annotations

import json
import logging
import os
from typing import List

from models.schemas import ProcessedEvent, UserProfile

logger = logging.getLogger(__name__)

_DEFAULT_LOG_PATH = "data/daily_log.json"


def should_send_push(event: ProcessedEvent, profile: UserProfile) -> bool:
    """Return True if event should trigger a push notification."""
    count = profile.push_management.current_count
    score = event.relevance_score

    if count >= 15:
        return False
    elif count >= 11:
        return score > 9.5
    elif count >= 6:
        return score > 9.0
    else:
        return score > 8.0


def log_to_daily_log(event: ProcessedEvent, log_path: str = _DEFAULT_LOG_PATH) -> None:
    """Append event to the daily log JSON file (creates file if absent)."""
    existing: List[dict] = []
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as exc:
            logger.warning("Could not read daily log (%s) — starting fresh", exc)

    existing.append({
        "event_id": event.event_id,
        "combined_summary": event.combined_summary,
        "relevance_score": event.relevance_score,
        "reasoning": event.reasoning,
    })

    tmp_path = log_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, log_path)
    logger.debug("Logged event %s to %s", event.event_id, log_path)


def route_event(
    event: ProcessedEvent,
    profile: UserProfile,
    log_path: str = _DEFAULT_LOG_PATH,
) -> bool:
    """Decide push vs. log. Returns True if notification should be sent."""
    count = profile.push_management.current_count

    if count >= 15:
        logger.info("Daily limit reached — event %s dropped", event.event_id)
        return False

    if should_send_push(event, profile):
        logger.info("Pushing event %s (score=%.2f)", event.event_id, event.relevance_score)
        return True

    log_to_daily_log(event, log_path=log_path)
    logger.info("Logged event %s to digest (score=%.2f)", event.event_id, event.relevance_score)
    return False
