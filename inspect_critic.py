"""Live inspection script for Day 2 — runs Scout → Critic and prints ProcessedEvents.

Usage:
    python3 inspect_critic.py              # first 15 articles (default)
    python3 inspect_critic.py --limit 5   # cheaper, faster run
    python3 inspect_critic.py --save      # also write data/critic_snapshot.json
    python3 inspect_critic.py --limit 5 --save
"""

import json
import os
import sys

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    sys.exit("ERROR: ANTHROPIC_API_KEY not set. Add it to .env or export it.")

# --- CLI args ---
limit = 15
save = False
args = sys.argv[1:]
if "--save" in args:
    save = True
    args.remove("--save")
if "--limit" in args:
    idx = args.index("--limit")
    limit = int(args[idx + 1])

# --- Imports (after env is ready) ---
from agents.critic import process_articles
from agents.scout import fetch_all
from models.schemas import UserProfile
from services.llm_client import LLMClient

# --- Load user profile ---
profile_path = "data/user_profile.json"
try:
    with open(profile_path, encoding="utf-8") as f:
        profile = UserProfile.model_validate(json.load(f))
except FileNotFoundError:
    profile = UserProfile()

# --- Fetch articles ---
print(f"Fetching articles from RSS feeds...")
articles = fetch_all()
print(f"Fetched {len(articles)} articles. Using first {limit} for Critic.\n")
articles = articles[:limit]

# --- Run Critic ---
client = LLMClient()
print("Running Critic (cluster → synthesise → score)...\n")
events = process_articles(articles, profile, client)

# --- Display results ---
sep = "━" * 60  # ━━━━
print(f"\n{sep}")
print(f"  {len(events)} events produced from {len(articles)} articles")
print(f"{sep}\n")

for i, event in enumerate(events, 1):
    short_id = event.event_id[:8]
    print(f"━━━ Event {i}/{len(events)}  id={short_id}  score={event.relevance_score:.1f}")
    print(f"Sources ({len(event.sources)}):")
    for src in event.sources:
        print(f"  [{src.source:<14}] {src.title}")
    print(f"\nSummary:")
    for line in event.combined_summary.splitlines():
        print(f"  {line}")
    reasoning_snippet = event.reasoning[:200].replace("\n", " ")
    print(f"\nReasoning: {reasoning_snippet}{'...' if len(event.reasoning) > 200 else ''}")
    print()

# --- Save snapshot ---
if save:
    out_path = "data/critic_snapshot.json"
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(
            [e.model_dump(mode="json") for e in events],
            f,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(tmp_path, out_path)
    print(f"Saved {len(events)} events to {out_path}")
