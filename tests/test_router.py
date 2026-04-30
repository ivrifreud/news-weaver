"""Tests for agents/router.py — top-10 daily push logic."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from agents.router import DAILY_PUSH_LIMIT, log_to_daily_log, route_event, should_send_push
from models.schemas import ProcessedEvent, PushManagement, RawArticle, UserProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(score: float) -> ProcessedEvent:
    article = RawArticle(
        title="כותרת",
        link="https://example.com/1",
        summary="תקציר בדיקה",
        source="ynet",
        published_at=datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc),
    )
    return ProcessedEvent(
        event_id="test-uuid-1234-5678-abcd",
        combined_summary="סיכום לבדיקה",
        relevance_score=score,
        reasoning="נימוק",
        sources=[article],
    )


def _make_profile(count: int) -> UserProfile:
    return UserProfile(push_management=PushManagement(current_count=count))


# ---------------------------------------------------------------------------
# should_send_push — budget-based (count < DAILY_PUSH_LIMIT)
# ---------------------------------------------------------------------------

class TestShouldSendPush:
    def test_accepted_when_count_zero(self):
        assert should_send_push(_make_event(1.0), _make_profile(0)) is True

    def test_accepted_any_score_within_budget(self):
        assert should_send_push(_make_event(1.0), _make_profile(5)) is True

    def test_accepted_at_count_just_below_limit(self):
        assert should_send_push(_make_event(1.0), _make_profile(DAILY_PUSH_LIMIT - 1)) is True

    def test_rejected_at_daily_limit(self):
        assert should_send_push(_make_event(10.0), _make_profile(DAILY_PUSH_LIMIT)) is False

    def test_rejected_above_daily_limit(self):
        assert should_send_push(_make_event(10.0), _make_profile(DAILY_PUSH_LIMIT + 5)) is False

    def test_boundary_one_below_vs_at_limit(self):
        assert should_send_push(_make_event(5.0), _make_profile(DAILY_PUSH_LIMIT - 1)) is True
        assert should_send_push(_make_event(5.0), _make_profile(DAILY_PUSH_LIMIT)) is False


# ---------------------------------------------------------------------------
# log_to_daily_log
# ---------------------------------------------------------------------------

class TestLogToDailyLog:
    def test_creates_log_file_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            log_to_daily_log(_make_event(7.0), log_path=path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        assert len(data) == 1
        assert data[0]["event_id"] == "test-uuid-1234-5678-abcd"

    def test_appends_to_existing_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            log_to_daily_log(_make_event(6.0), log_path=path)
            log_to_daily_log(_make_event(7.0), log_path=path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        assert len(data) == 2

    def test_log_contains_hebrew_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            log_to_daily_log(_make_event(5.0), log_path=path)
            with open(path, encoding="utf-8") as f:
                content = f.read()
        assert "סיכום לבדיקה" in content


# ---------------------------------------------------------------------------
# route_event
# ---------------------------------------------------------------------------

class TestRouteEvent:
    def test_returns_true_within_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            result = route_event(_make_event(5.0), _make_profile(0), log_path=path)
        assert result is True

    def test_returns_false_and_logs_when_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            result = route_event(_make_event(9.9), _make_profile(DAILY_PUSH_LIMIT), log_path=path)
            assert result is False
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        assert len(data) == 1

    def test_top_10_all_sent(self):
        """First 10 events pushed, 11th logged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            pushed = 0
            for i in range(11):
                profile = _make_profile(pushed)
                result = route_event(_make_event(max(1.0, min(10.0, float(10 - i) + 1))), profile, log_path=path)
                if result:
                    pushed += 1
            assert pushed == DAILY_PUSH_LIMIT
            with open(path, encoding="utf-8") as f:
                logged = json.load(f)
        assert len(logged) == 1
