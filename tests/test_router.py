"""Tests for agents/router.py — all boundary conditions for dynamic thresholds."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from agents.router import log_to_daily_log, route_event, should_send_push
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
# should_send_push — low count (< 6): threshold > 8.0
# ---------------------------------------------------------------------------

class TestThresholdLowCount:
    def test_score_exactly_at_threshold_rejected(self):
        assert should_send_push(_make_event(8.0), _make_profile(0)) is False

    def test_score_just_above_threshold_accepted(self):
        assert should_send_push(_make_event(8.01), _make_profile(0)) is True

    def test_score_well_above_threshold_accepted(self):
        assert should_send_push(_make_event(9.9), _make_profile(5)) is True

    def test_count_5_still_uses_low_threshold(self):
        assert should_send_push(_make_event(8.5), _make_profile(5)) is True

    def test_count_5_score_at_threshold_rejected(self):
        assert should_send_push(_make_event(8.0), _make_profile(5)) is False


# ---------------------------------------------------------------------------
# should_send_push — mid count (6–10): threshold > 9.0
# ---------------------------------------------------------------------------

class TestThresholdMidCount:
    def test_count_6_score_below_new_threshold_rejected(self):
        # score 8.5 passed at count=5 but must fail at count=6
        assert should_send_push(_make_event(8.5), _make_profile(6)) is False

    def test_count_6_score_exactly_at_threshold_rejected(self):
        assert should_send_push(_make_event(9.0), _make_profile(6)) is False

    def test_count_6_score_just_above_threshold_accepted(self):
        assert should_send_push(_make_event(9.01), _make_profile(6)) is True

    def test_count_10_score_above_threshold_accepted(self):
        assert should_send_push(_make_event(9.5), _make_profile(10)) is True

    def test_count_10_score_at_threshold_rejected(self):
        assert should_send_push(_make_event(9.0), _make_profile(10)) is False

    def test_boundary_count_5_vs_6(self):
        """Score 8.5 passes at count=5 but fails at count=6."""
        assert should_send_push(_make_event(8.5), _make_profile(5)) is True
        assert should_send_push(_make_event(8.5), _make_profile(6)) is False


# ---------------------------------------------------------------------------
# should_send_push — high count (11–14): threshold > 9.5
# ---------------------------------------------------------------------------

class TestThresholdHighCount:
    def test_count_11_score_below_mid_threshold_rejected(self):
        assert should_send_push(_make_event(9.0), _make_profile(11)) is False

    def test_count_11_score_exactly_at_threshold_rejected(self):
        assert should_send_push(_make_event(9.5), _make_profile(11)) is False

    def test_count_11_score_just_above_threshold_accepted(self):
        assert should_send_push(_make_event(9.51), _make_profile(11)) is True

    def test_count_14_score_max_accepted(self):
        assert should_send_push(_make_event(9.99), _make_profile(14)) is True

    def test_boundary_count_10_vs_11(self):
        """Score 9.1 passes at count=10 but fails at count=11."""
        assert should_send_push(_make_event(9.1), _make_profile(10)) is True
        assert should_send_push(_make_event(9.1), _make_profile(11)) is False


# ---------------------------------------------------------------------------
# should_send_push — daily limit reached (>= 15): always False
# ---------------------------------------------------------------------------

class TestDailyLimit:
    def test_count_15_always_rejected(self):
        assert should_send_push(_make_event(10.0), _make_profile(15)) is False

    def test_count_exceeded_rejected(self):
        assert should_send_push(_make_event(10.0), _make_profile(20)) is False

    def test_boundary_count_14_vs_15(self):
        """Score 9.99 passes at count=14 but fails at count=15."""
        assert should_send_push(_make_event(9.99), _make_profile(14)) is True
        assert should_send_push(_make_event(9.99), _make_profile(15)) is False


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
        assert "סיכום לבדיקה" in content  # Hebrew text preserved


# ---------------------------------------------------------------------------
# route_event
# ---------------------------------------------------------------------------

class TestRouteEvent:
    def test_returns_true_when_should_push(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            result = route_event(_make_event(9.0), _make_profile(0), log_path=path)
        assert result is True

    def test_returns_false_and_logs_below_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            result = route_event(_make_event(5.0), _make_profile(3), log_path=path)
            assert result is False
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        assert len(data) == 1

    def test_returns_false_and_does_not_log_at_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "daily_log.json")
            result = route_event(_make_event(10.0), _make_profile(15), log_path=path)
            assert result is False
            assert not os.path.exists(path)  # no log file created
