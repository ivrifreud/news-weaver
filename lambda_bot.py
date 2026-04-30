"""AWS Lambda handler — processes one Telegram update delivered via webhook."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from telegram import Update
from agents.learner import build_application

logger = logging.getLogger(__name__)

# Module-level app — reused across warm Lambda invocations
_app = None


async def _process(body: str) -> None:
    global _app
    if _app is None:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        _app = build_application(token)
        await _app.initialize()
        await _app.start()
        logger.info("PTB Application initialized")

    data = json.loads(body)
    update = Update.de_json(data, _app.bot)
    await _app.process_update(update)


def handler(event: dict, context: object) -> dict:
    """API Gateway → Lambda entry point. Processes exactly one Telegram Update."""
    body = event.get("body") or "{}"
    try:
        asyncio.run(_process(body))
    except Exception as exc:
        logger.error("Failed to process update: %s", exc)
        # Return 200 always — Telegram retries on non-2xx, causing duplicate processing
    return {"statusCode": 200, "body": "ok"}
