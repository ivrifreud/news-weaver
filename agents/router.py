"""Node 3 — The Router: send top 10 events by relevance score, log the rest."""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from models.schemas import ProcessedEvent, UserProfile

logger = logging.getLogger(__name__)

DAILY_PUSH_LIMIT = 10
_DAILY_LOG_KEY = "data/daily_log.json"


def should_send_push(event: ProcessedEvent, profile: UserProfile) -> bool:
    """Return True if daily push budget not yet exhausted."""
    return profile.push_management.current_count < DAILY_PUSH_LIMIT


def log_to_daily_log(event: ProcessedEvent, log_path: Optional[str] = None) -> None:
    """Append event to the daily log.

    Uses the storage adapter (S3 in Lambda, local file in dev) when log_path is
    None. Passing an explicit log_path bypasses the adapter — used in tests.
    """
    entry = {
        "event_id": event.event_id,
        "combined_summary": event.combined_summary,
        "relevance_score": event.relevance_score,
        "reasoning": event.reasoning,
    }

    if log_path is not None:
        # Test path: direct local file I/O
        existing: List[dict] = []
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as exc:
                logger.warning("Could not read daily log (%s) — starting fresh", exc)
        existing.append(entry)
        tmp_path = log_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, log_path)
    else:
        # Production path: storage adapter handles S3 vs local transparently
        from services import storage
        existing = storage.read_json(_DAILY_LOG_KEY, default=[])
        if not isinstance(existing, list):
            existing = []
        existing.append(entry)
        storage.write_json(_DAILY_LOG_KEY, existing)

    logger.debug("Logged event %s to daily log", event.event_id)


def route_event(
    event: ProcessedEvent,
    profile: UserProfile,
    log_path: Optional[str] = None,
) -> bool:
    """Push if within top-10 budget, otherwise log. Returns True if pushed."""
    if should_send_push(event, profile):
        logger.info("Pushing event %s (score=%.2f, count=%d/%d)",
                    event.event_id, event.relevance_score,
                    profile.push_management.current_count + 1, DAILY_PUSH_LIMIT)
        return True

    log_to_daily_log(event, log_path=log_path)
    logger.info("Logged event %s to digest (score=%.2f)", event.event_id, event.relevance_score)
    return False
