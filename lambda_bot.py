"""AWS Lambda handler — processes one Telegram update delivered via webhook."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from telegram import Bot, Update
from agents.learner import handle_expand, handle_feedback

logger = logging.getLogger(__name__)


async def _process(body: str) -> None:
    data = json.loads(body)
    if not data.get("update_id"):
        logger.warning("No update_id in body — skipping (not a Telegram update)")
        return

    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    update = Update.de_json(data, bot)
    if not update or not update.callback_query:
        logger.debug("Update has no callback_query — nothing to handle")
        return

    callback_data = update.callback_query.data or ""
    if callback_data.startswith(("like:", "dislike:")):
        await handle_feedback(update, None)
    elif callback_data.startswith("expand:"):
        await handle_expand(update, None)
    else:
        logger.warning("Unhandled callback_data: %s", callback_data)


def handler(event: dict, context: object) -> dict:
    """API Gateway → Lambda entry point. Processes exactly one Telegram Update."""
    body = event.get("body") or "{}"
    try:
        asyncio.run(_process(body))
    except Exception as exc:
        logger.error("Failed to process update: %s", exc)
        # Always return 200 — Telegram retries on non-2xx causing duplicates
    return {"statusCode": 200, "body": "ok"}
