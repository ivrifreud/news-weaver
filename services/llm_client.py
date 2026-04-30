"""Anthropic SDK wrapper used by all agents."""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


class LLMClient:
    """Thin wrapper around the Anthropic Messages API."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            max_retries=3,
        )
        self._usage_log: List[Dict] = []

    def call(self, prompt: str, max_tokens: int = 1000,
             model: Optional[str] = None) -> str:
        """Send a single-turn prompt and return the text response."""
        used_model = model or MODEL
        try:
            response = self._client.messages.create(
                model=used_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            self._usage_log.append({
                "model": used_model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            })
            return response.content[0].text
        except anthropic.APIError as exc:
            logger.error("Anthropic API error: %s", exc)
            raise

    def get_usage(self) -> List[Dict]:
        """Return accumulated token usage records."""
        return list(self._usage_log)

    def reset_usage(self) -> None:
        """Clear the usage log."""
        self._usage_log.clear()
