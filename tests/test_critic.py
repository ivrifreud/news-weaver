"""Tests for agents/critic.py — all LLM calls are mocked."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock

import pytest

from agents.critic import (
    _cluster_articles,
    _extract_thinking,
    _score_event,
    _strip_fences,
    _synthesize_cluster,
    process_articles,
)
from models.schemas import ProcessedEvent, RawArticle, UserProfile, Interest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client() -> MagicMock:
    """A MagicMock that stands in for LLMClient."""
    return MagicMock()


@pytest.fixture
def sample_articles() -> List[RawArticle]:
    base = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
    return [
        RawArticle(title="נתניהו בבג\"ץ", link="https://ynet.co.il/1",
                   summary="ראש הממשלה העיד היום", source="ynet", published_at=base),
        RawArticle(title="נתניהו חזר להעיד", link="https://israelhayom.co.il/1",
                   summary="לאחר הפסקה חזר נתניהו", source="israel_hayom", published_at=base),
        RawArticle(title="מניות הטק עולות", link="https://themarker.com/1",
                   summary="מדד הנאסד\"ק עלה היום", source="the_marker", published_at=base),
    ]


@pytest.fixture
def sample_profile() -> UserProfile:
    return UserProfile(
        interests=[Interest(topic="פוליטיקה", weight=0.9)],
        avoid_topics=["ספורט"],
    )


# ---------------------------------------------------------------------------
# _strip_fences
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_strips_json_fences(self):
        text = '```json\n{"key": "value"}\n```'
        assert _strip_fences(text) == '{"key": "value"}'

    def test_strips_plain_fences(self):
        text = '```\n{"key": "value"}\n```'
        assert _strip_fences(text) == '{"key": "value"}'

    def test_plain_json_unchanged(self):
        text = '{"key": "value"}'
        assert _strip_fences(text) == '{"key": "value"}'

    def test_strips_surrounding_whitespace(self):
        text = '  ```json\n{"a": 1}\n```  '
        assert _strip_fences(text) == '{"a": 1}'

    def test_multiline_json_preserved(self):
        inner = '{\n  "a": 1,\n  "b": 2\n}'
        text = f'```json\n{inner}\n```'
        assert _strip_fences(text) == inner


# ---------------------------------------------------------------------------
# _extract_thinking
# ---------------------------------------------------------------------------

class TestExtractThinking:
    def test_extracts_thinking_block(self):
        text = "<thinking>\nניתוח מעמיק של האירוע\n</thinking>\nסיכום בעברית."
        thinking, remainder = _extract_thinking(text)
        assert thinking == "ניתוח מעמיק של האירוע"
        assert "סיכום בעברית" in remainder
        assert "<thinking>" not in remainder
        assert "</thinking>" not in remainder

    def test_no_thinking_block_returns_empty(self):
        text = "סיכום בעברית ללא thinking"
        thinking, remainder = _extract_thinking(text)
        assert thinking == ""
        assert remainder == text

    def test_multiline_thinking(self):
        text = "<thinking>\nשורה אחת\nשורה שניה\n</thinking>\nסיכום."
        thinking, _ = _extract_thinking(text)
        assert "שורה אחת" in thinking
        assert "שורה שניה" in thinking

    def test_remainder_is_stripped(self):
        text = "<thinking>מחשבה</thinking>  \n  סיכום נקי  "
        _, remainder = _extract_thinking(text)
        assert remainder == "סיכום נקי"


# ---------------------------------------------------------------------------
# _cluster_articles
# ---------------------------------------------------------------------------

class TestClusterArticles:
    def test_clusters_into_correct_groups(self, mock_client, sample_articles):
        mock_client.call.return_value = json.dumps({
            "clusters": [
                {"event_id": "e1", "article_indices": [0, 1]},
                {"event_id": "e2", "article_indices": [2]},
            ]
        })
        groups = _cluster_articles(sample_articles, mock_client)
        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 1

    def test_handles_fenced_json(self, mock_client, sample_articles):
        payload = json.dumps({
            "clusters": [{"event_id": "e1", "article_indices": [0, 1, 2]}]
        })
        mock_client.call.return_value = f"```json\n{payload}\n```"
        groups = _cluster_articles(sample_articles, mock_client)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_fallback_to_singletons_on_malformed_json(self, mock_client, sample_articles):
        mock_client.call.return_value = "definitely not valid json"
        groups = _cluster_articles(sample_articles, mock_client)
        assert len(groups) == len(sample_articles)
        assert all(len(g) == 1 for g in groups)

    def test_unassigned_articles_become_singletons(self, mock_client, sample_articles):
        # Cluster only assigns article 0; articles 1 and 2 should become singletons
        mock_client.call.return_value = json.dumps({
            "clusters": [{"event_id": "e1", "article_indices": [0]}]
        })
        groups = _cluster_articles(sample_articles, mock_client)
        assert len(groups) == 3  # 1 assigned + 2 singletons

    def test_empty_articles_returns_empty(self, mock_client):
        groups = _cluster_articles([], mock_client)
        assert groups == []
        mock_client.call.assert_not_called()

    def test_ignores_out_of_range_indices(self, mock_client, sample_articles):
        mock_client.call.return_value = json.dumps({
            "clusters": [{"event_id": "e1", "article_indices": [0, 99]}]
        })
        groups = _cluster_articles(sample_articles, mock_client)
        # article 0 assigned; articles 1, 2 become singletons; index 99 skipped
        total_articles = sum(len(g) for g in groups)
        assert total_articles == len(sample_articles)


# ---------------------------------------------------------------------------
# _synthesize_cluster
# ---------------------------------------------------------------------------

class TestSynthesizeCluster:
    def test_returns_summary_and_reasoning(self, mock_client, sample_articles, sample_profile):
        mock_client.call.return_value = (
            "<thinking>\nניתוח מעמיק: שני מקורות מדווחים על אותו אירוע.\n</thinking>\n"
            "נתניהו חזר היום להעיד בבית המשפט. שני מקורות אישרו את הדיווח."
        )
        summary, reasoning = _synthesize_cluster(sample_articles[:2], sample_profile, mock_client)
        assert "נתניהו" in summary
        assert "ניתוח מעמיק" in reasoning
        assert "<thinking>" not in summary
        assert "<thinking>" not in reasoning

    def test_empty_reasoning_when_no_thinking_tag(self, mock_client, sample_articles, sample_profile):
        mock_client.call.return_value = "סיכום ישיר ללא תגיות thinking."
        summary, reasoning = _synthesize_cluster(sample_articles[:1], sample_profile, mock_client)
        assert reasoning == ""
        assert summary == "סיכום ישיר ללא תגיות thinking."

    def test_uses_synthesis_max_tokens(self, mock_client, sample_articles, sample_profile):
        mock_client.call.return_value = "<thinking>t</thinking>\nסיכום."
        _synthesize_cluster(sample_articles[:1], sample_profile, mock_client)
        _, kwargs = mock_client.call.call_args
        assert kwargs.get("max_tokens") == 2000

    def test_fallback_on_api_error(self, mock_client, sample_articles, sample_profile):
        mock_client.call.side_effect = Exception("API error")
        summary, reasoning = _synthesize_cluster(sample_articles[:1], sample_profile, mock_client)
        # Falls back to first article's summary
        assert summary == sample_articles[0].summary
        assert reasoning == ""


# ---------------------------------------------------------------------------
# _score_event
# ---------------------------------------------------------------------------

class TestScoreEvent:
    def test_parses_score_and_reasoning(self, mock_client, sample_profile):
        mock_client.call.return_value = '{"relevance_score": 8.5, "reasoning": "רלוונטי מאוד"}'
        score, reasoning = _score_event("some Hebrew summary", sample_profile, mock_client)
        assert score == 8.5
        assert reasoning == "רלוונטי מאוד"

    def test_handles_fenced_score_json(self, mock_client, sample_profile):
        payload = '{"relevance_score": 7.0, "reasoning": "בינוני"}'
        mock_client.call.return_value = f"```json\n{payload}\n```"
        score, _ = _score_event("summary", sample_profile, mock_client, sources=[])
        assert score == 7.0

    def test_clamps_score_above_max(self, mock_client, sample_profile):
        mock_client.call.return_value = '{"relevance_score": 15.0, "reasoning": "גבוה מדי"}'
        score, _ = _score_event("summary", sample_profile, mock_client, sources=[])
        assert score == 10.0

    def test_clamps_score_below_min(self, mock_client, sample_profile):
        mock_client.call.return_value = '{"relevance_score": -3.0, "reasoning": "נמוך מדי"}'
        score, _ = _score_event("summary", sample_profile, mock_client, sources=[])
        assert score == 1.0

    def test_default_score_on_malformed_json(self, mock_client, sample_profile):
        mock_client.call.return_value = "not json"
        score, reasoning = _score_event("summary", sample_profile, mock_client, sources=[])
        assert score == 5.0
        assert reasoning == ""

    def test_uses_scoring_max_tokens(self, mock_client, sample_profile):
        mock_client.call.return_value = '{"relevance_score": 5.0, "reasoning": "ok"}'
        _score_event("summary", sample_profile, mock_client, sources=[])
        _, kwargs = mock_client.call.call_args
        assert kwargs.get("max_tokens") == 500


# ---------------------------------------------------------------------------
# process_articles (full pipeline)
# ---------------------------------------------------------------------------

class TestProcessArticles:
    def test_full_pipeline_single_cluster(self, mock_client, sample_articles, sample_profile):
        mock_client.call.side_effect = [
            json.dumps({"clusters": [{"event_id": "e1", "article_indices": [0, 1, 2]}]}),
            # quick-score response (Haiku)
            json.dumps({"scores": [{"cluster_id": 0, "score": 7.5}]}),
            # merged synthesis+score response
            "<thinking>ניתוח האירוע</thinking>\nנתניהו העיד היום בבית המשפט."
            '\n<score>{"relevance_score": 7.5, "reasoning": "רלוונטי לפוליטיקה"}</score>',
        ]
        events = process_articles(sample_articles, sample_profile, mock_client)
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ProcessedEvent)
        assert event.relevance_score == 7.5
        assert "נתניהו" in event.combined_summary
        assert "ניתוח האירוע" in event.reasoning
        assert len(event.sources) == 3

    def test_full_pipeline_two_clusters(self, mock_client, sample_articles, sample_profile):
        mock_client.call.side_effect = [
            # Step 1: clustering → 2 groups
            json.dumps({"clusters": [
                {"event_id": "e1", "article_indices": [0, 1]},
                {"event_id": "e2", "article_indices": [2]},
            ]}),
            # Step 2: quick-score response (Haiku)
            json.dumps({"scores": [{"cluster_id": 0, "score": 9.0}, {"cluster_id": 1, "score": 6.0}]}),
            # Step 3a: merged synthesis+score cluster 1
            "<thinking>פוליטיקה</thinking>\nנתניהו העיד."
            '\n<score>{"relevance_score": 9.0, "reasoning": "פוליטיקה חשובה"}</score>',
            # Step 3b: merged synthesis+score cluster 2
            "<thinking>כלכלה</thinking>\nמניות הטק עלו."
            '\n<score>{"relevance_score": 6.0, "reasoning": "כלכלה רלוונטית"}</score>',
        ]
        events = process_articles(sample_articles, sample_profile, mock_client)
        assert len(events) == 2
        scores = {e.relevance_score for e in events}
        assert scores == {9.0, 6.0}

    def test_event_ids_are_valid_uuids(self, mock_client, sample_articles, sample_profile):
        mock_client.call.side_effect = [
            json.dumps({"clusters": [{"event_id": "e1", "article_indices": [0, 1, 2]}]}),
            json.dumps({"scores": [{"cluster_id": 0, "score": 5.0}]}),
            '<thinking>t</thinking>\nסיכום.\n<score>{"relevance_score": 5.0, "reasoning": "ok"}</score>',
        ]
        events = process_articles(sample_articles, sample_profile, mock_client)
        assert len(events) == 1
        uuid.UUID(events[0].event_id)  # raises ValueError if not a valid UUID

    def test_empty_articles_returns_empty(self, mock_client, sample_profile):
        events = process_articles([], sample_profile, mock_client)
        assert events == []
        mock_client.call.assert_not_called()

    def test_scoring_prompt_includes_prominence(self, mock_client, sample_profile):
        """Top-headline articles should surface in the scoring prompt."""
        top_article = RawArticle(
            title="כותרת ראשית", link="https://ynet.co.il/top",
            summary="תקציר", source="ynet",
            published_at=datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc),
            position_in_feed=1,
        )
        mock_client.call.return_value = '{"relevance_score": 9.0, "reasoning": "כותרת ראשית"}'
        _score_event("summary", sample_profile, mock_client, sources=[top_article])
        prompt_used = mock_client.call.call_args[0][0]
        assert "top_headline_sources" in prompt_used
        assert "ynet" in prompt_used

    def test_reasoning_includes_thinking_and_score(self, mock_client, sample_articles, sample_profile):
        mock_client.call.side_effect = [
            json.dumps({"clusters": [{"event_id": "e1", "article_indices": [0]}]}),
            json.dumps({"scores": [{"cluster_id": 0, "score": 8.0}]}),
            "<thinking>ניתוח עמוק</thinking>\nסיכום."
            '\n<score>{"relevance_score": 8.0, "reasoning": "הסבר ציון"}</score>',
        ]
        events = process_articles(sample_articles[:1], sample_profile, mock_client)
        assert "ניתוח עמוק" in events[0].reasoning
        assert "הסבר ציון" in events[0].reasoning
