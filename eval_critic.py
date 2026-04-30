"""Prompt eval — runs 3 fixed clusters and prints quality/cost metrics.

Usage:
    python3 eval_critic.py

Prints one summary block per cluster:
  cluster name | articles | API calls | tokens in/out | score | summary snippet
"""

from __future__ import annotations

import json
import os
import sys
import time

import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone

from agents.critic import process_articles
from models.schemas import RawArticle, UserProfile, Interest
from services.llm_client import LLMClient

# ---------------------------------------------------------------------------
# Fixed eval clusters (article indices into articles_snapshot.json)
# Chosen to cover: high-relevance political, high-relevance tech, lower-relevance
# ---------------------------------------------------------------------------

EVAL_CLUSTERS_INDICES = [
    {
        "name": "Netanyahu / Politics",
        "desc": "multi-source political event — should score high",
        "indices": [9, 11, 40],   # נתניהו העיד / קטאר לתובע בהאג / כן ביבי לא ביבי
    },
    {
        "name": "Google Feature / Tech",
        "desc": "tech feature across 3 sources — should score high",
        "indices": [36, 41, 136],  # israel_hayom x2 + geektime
    },
    {
        "name": "AI & Cybersecurity",
        "desc": "AI security story — should score high for this profile",
        "indices": [135, 127],    # geektime + the_marker
    },
]

SNAPSHOT_PATH = "data/articles_snapshot.json"
PROFILE_PATH = "data/user_profile.json"


def load_article(raw: dict) -> RawArticle:
    """Build a RawArticle from snapshot dict (adds position_in_feed=0 default)."""
    return RawArticle(
        title=raw["title"],
        link=raw["link"],
        summary=raw["summary"],
        source=raw["source"],
        published_at=datetime.fromisoformat(raw["published_at"].replace("Z", "+00:00")),
        position_in_feed=raw.get("position_in_feed", 0),
    )


def main() -> None:
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        all_articles_raw = json.load(f)

    with open(PROFILE_PATH, encoding="utf-8") as f:
        profile = UserProfile.model_validate(json.load(f))

    print(f"\n{'='*72}")
    print(f"  PROMPT EVAL — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  Model: see per-call breakdown | Profile interests: {len(profile.interests)}")
    print(f"{'='*72}\n")

    total_in = 0
    total_out = 0
    total_calls = 0

    for cluster_def in EVAL_CLUSTERS_INDICES:
        client = LLMClient()
        articles = [load_article(all_articles_raw[i]) for i in cluster_def["indices"]]

        t0 = time.time()
        events = process_articles(articles, profile, client)
        elapsed = time.time() - t0

        usage = client.get_usage()
        calls = len(usage)
        in_tok = sum(u["input_tokens"] for u in usage)
        out_tok = sum(u["output_tokens"] for u in usage)
        models_used = ", ".join(sorted({u["model"] for u in usage}))

        total_in += in_tok
        total_out += out_tok
        total_calls += calls

        print(f"  Cluster : {cluster_def['name']}")
        print(f"  Desc    : {cluster_def['desc']}")
        print(f"  Articles: {len(articles)} — {', '.join(a.title[:35] for a in articles)}")
        print(f"  Model(s): {models_used}")
        print(f"  Calls   : {calls}  |  Tokens: {in_tok} in / {out_tok} out  |  {elapsed:.1f}s")

        if events:
            e = events[0]
            print(f"  Score   : {e.relevance_score:.1f}")
            print(f"  Summary : {e.combined_summary[:120]}…")
            print(f"  Reasoning snippet: {e.reasoning[:100]}…")
        else:
            print("  [no events produced]")

        print()

    print(f"{'─'*72}")
    print(f"  TOTAL   : {total_calls} calls | {total_in} tokens in / {total_out} tokens out")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
