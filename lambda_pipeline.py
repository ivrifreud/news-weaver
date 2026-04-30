"""AWS Lambda handler — runs the daily news pipeline."""

from __future__ import annotations

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

from main import run_pipeline


def handler(event: dict, context: object) -> dict:
    """EventBridge → Lambda entry point."""
    run_pipeline()
    return {"statusCode": 200, "body": "pipeline complete"}
