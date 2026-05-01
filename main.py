"""Entry point — wires Scout → Critic → Router → Telegram push."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler

from agents.critic import process_articles
from agents.learner import build_application, process_feedback_queue
from agents.router import route_event
from agents.scout import fetch_all
from models.schemas import UserProfile
from services import storage
from services.llm_client import LLMClient
from services.telegram_bot import send_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_PROFILE_PATH = "data/user_profile.json"
_EVENTS_CACHE_PATH = "data/events_cache.json"


# ---------------------------------------------------------------------------
# Profile I/O
# ---------------------------------------------------------------------------

def _load_profile() -> UserProfile:
    data = storage.read_json(_PROFILE_PATH, default={})
    return UserProfile.model_validate(data) if data else UserProfile()


def _save_profile(profile: UserProfile) -> None:
    storage.write_json(_PROFILE_PATH, profile.model_dump(mode="json"))


def _update_events_cache(events_cache: dict) -> None:
    storage.write_json(_EVENTS_CACHE_PATH, events_cache)


def _reset_daily_count_if_needed() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    profile = _load_profile()
    if profile.push_management.last_reset_date != today:
        profile.push_management.current_count = 0
        profile.push_management.last_reset_date = today
        _save_profile(profile)
        logger.info("Daily push count reset for %s", today)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    bot_loop: Optional[asyncio.AbstractEventLoop] = None,
    bot_app=None,
) -> None:
    """Fetch → cluster/score → route → push. Called by scheduler or --run-once."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    llm_client = LLMClient()

    lambda_mode = bool(token and not bot_loop)

    logger.info("Pipeline started")

    _reset_daily_count_if_needed()

    # Drain any feedback queued since last run (survives system restarts)
    drained = process_feedback_queue()
    if drained:
        logger.info("Applied %d queued feedback items before pipeline run", drained)

    articles = fetch_all()
    logger.info("Fetched %d articles", len(articles))

    if not articles:
        return

    profile = _load_profile()
    events = process_articles(articles, profile, llm_client)
    logger.info("Produced %d events", len(events))

    events_cache: dict = storage.read_json(_EVENTS_CACHE_PATH, default={})

    for event in sorted(events, key=lambda e: e.relevance_score, reverse=True):
        profile = _load_profile()  # re-read in case count changed
        should_push = route_event(event, profile)

        if should_push:
            # Cache summary + source URLs for feedback weighting and expand
            events_cache[event.event_id] = {
                "summary": event.combined_summary[:200],
                "sources": [
                    {"link": a.link, "title": a.title, "source": a.source}
                    for a in event.sources
                ],
            }

            profile.push_management.current_count += 1
            _save_profile(profile)

            if bot_loop and bot_app and chat_id:
                # scheduler mode: send via long-lived PTB application thread
                future = asyncio.run_coroutine_threadsafe(
                    send_notification(bot_app.bot, chat_id, event),
                    bot_loop,
                )
                try:
                    future.result(timeout=15)
                except Exception as exc:
                    logger.error("Telegram send failed: %s", exc)
            elif lambda_mode and chat_id:
                # Lambda mode: fresh Bot per send so each asyncio.run() gets
                # a clean HTTP session (reusing one Bot across asyncio.run()
                # calls closes the session after the first invocation)
                async def _send(tok: str, cid: str, ev=event) -> None:
                    from telegram import Bot
                    async with Bot(token=tok) as bot:
                        await send_notification(bot, cid, ev)
                try:
                    asyncio.run(_send(token, chat_id))
                except Exception as exc:
                    logger.error("Telegram send failed: %s", exc)
            else:
                logger.info(
                    "[DRY RUN] Would push event %s: %.60s…",
                    event.event_id,
                    event.combined_summary,
                )

    _update_events_cache(events_cache)
    logger.info("Pipeline complete")


# ---------------------------------------------------------------------------
# Bot thread
# ---------------------------------------------------------------------------

def _start_bot_thread(token: str) -> tuple:
    """Start the PTB application in a daemon thread. Returns (loop, app).

    The Application must be built inside the thread after set_event_loop() so
    all PTB-internal Futures are bound to the correct loop (Python 3.8 is strict
    about Future-loop coupling).
    """
    loop = asyncio.new_event_loop()
    app_ref: list = [None]
    ready = threading.Event()

    async def _run_app() -> None:
        app = build_application(token)
        app_ref[0] = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        ready.set()
        await asyncio.Event().wait()  # block forever in this loop

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_app())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(timeout=30)
    return loop, app_ref[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="News Weaver")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-once", action="store_true", help="Run pipeline once and exit")
    group.add_argument("--scheduler", action="store_true", help="Run on 30-minute schedule")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    bot_loop: Optional[asyncio.AbstractEventLoop] = None
    bot_app = None

    if token:
        logger.info("Starting Telegram bot…")
        bot_loop, bot_app = _start_bot_thread(token)
        logger.info("Bot is polling")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — running in dry-run mode")

    if args.run_once:
        run_pipeline(bot_loop=bot_loop, bot_app=bot_app)
        sys.exit(0)

    # --scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_pipeline,
        "cron",
        hour=7,
        minute=0,
        kwargs={"bot_loop": bot_loop, "bot_app": bot_app},
        id="pipeline",
    )
    scheduler.start()
    logger.info("Scheduler started — running daily at 07:00 UTC. Press Ctrl-C to stop.")

    try:
        # Run immediately on start
        run_pipeline(bot_loop=bot_loop, bot_app=bot_app)
        threading.Event().wait()  # block forever
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
