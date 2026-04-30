"""Quick inspection script — prints fetched articles and optionally saves a snapshot.

Usage:
    python3 inspect_articles.py              # print all articles
    python3 inspect_articles.py --save       # also save to data/articles_snapshot.json
"""

import json
import os
import sys

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from agents.scout import fetch_all

save = "--save" in sys.argv

articles = fetch_all()

print(f"\nFetched {len(articles)} articles in the last 6 hours:\n")
for a in articles:
    print(f"[{a.source:<12}] pos={a.position_in_feed:<3} {a.published_at:%H:%M}  {a.title}")
    print(f"  summary : {a.summary[:150]}")
    print()

if save:
    out_path = "data/articles_snapshot.json"
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump([a.model_dump(mode="json") for a in articles], f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)
    print(f"Saved {len(articles)} articles to {out_path}")
