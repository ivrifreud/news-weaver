"""Pydantic models for News Weaver."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RawArticle(BaseModel):
    """A single article fetched from an RSS feed."""

    model_config = ConfigDict(strict=True)

    title: str
    link: str
    summary: str  # clean text: RSS excerpt or HTML-extracted fallback
    source: str  # e.g. "ynet", "israel_hayom", "the_marker", "geektime"
    published_at: datetime
    position_in_feed: int = 0  # 1-based rank in the source feed; 1 = top headline


class Interest(BaseModel):
    """A user interest topic with a relevance weight."""

    model_config = ConfigDict(strict=True)

    topic: str
    weight: float = Field(ge=0.0, le=1.0)
    sub_topics: List[str] = []


class BehaviorMemory(BaseModel):
    """Accumulated signals from user feedback on past events."""

    model_config = ConfigDict(strict=True)

    positive_signals: List[str] = []  # event_ids the user liked
    negative_signals: List[str] = []  # event_ids the user disliked


class PushManagement(BaseModel):
    """Controls daily push notification budget and dynamic threshold."""

    model_config = ConfigDict(strict=True)

    daily_limit: int = 15
    current_count: int = 0
    dynamic_threshold: float = 8.0
    last_reset_date: Optional[str] = None  # ISO date YYYY-MM-DD, UTC


class UserProfile(BaseModel):
    """Persisted user preferences, interests, and notification state."""

    model_config = ConfigDict(strict=True)

    interests: List[Interest] = []
    avoid_topics: List[str] = []
    behavior_memory: BehaviorMemory = BehaviorMemory()
    push_management: PushManagement = PushManagement()


class ProcessedEvent(BaseModel):
    """A clustered and summarized news event ready for routing."""

    model_config = ConfigDict(strict=True)

    event_id: str  # uuid4
    combined_summary: str  # 3-4 sentences in Hebrew
    relevance_score: float = Field(ge=1.0, le=10.0)
    reasoning: str  # CoT output from Critic
    sources: List[RawArticle]
