# CLAUDE.md — The News Weaver (Agentic News Aggregator)

> This file is read automatically by Claude Code at the start of every session.
> It contains the full project context, architecture, coding conventions, and sprint plan.

---

## 🎯 Project Goal

Build a Python-based intelligent news aggregator that:
- Fetches Hebrew news from RSS feeds (Ynet, Israel Hayom, The Marker, Geektime)
- Clusters related stories into unified events
- Generates multi-perspective Hebrew summaries via Claude claude-sonnet-4-5
- Sends personalized push notifications (max 15/day) via Telegram

---

## 🗂️ Repository Structure (target)

```
news-weaver/
├── CLAUDE.md                  ← you are here
├── .env                       ← secrets (never commit)
├── .env.example               ← template for secrets
├── requirements.txt
├── pyproject.toml             ← optional, for tooling config
│
├── main.py                    ← entrypoint / scheduler
│
├── models/
│   └── schemas.py             ← all Pydantic models
│
├── agents/
│   ├── scout.py               ← Node 1: RSS ingestion
│   ├── critic.py              ← Node 2: clustering + scoring
│   ├── router.py              ← Node 3: dynamic thresholding
│   └── learner.py             ← Node 4: feedback loop
│
├── services/
│   ├── telegram_bot.py        ← Telegram Bot API wrapper
│   └── llm_client.py          ← Anthropic SDK wrapper
│
├── data/
│   ├── user_profile.json      ← persisted UserProfile
│   └── daily_log.json         ← daily events log
│
└── tests/
    ├── test_scout.py
    ├── test_critic.py
    └── test_router.py
```

---

## 🧱 Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11+ |
| LLM | Claude claude-sonnet-4-5 via `anthropic` SDK |
| Data validation | Pydantic v2 |
| Storage | Local JSON files |
| Messaging | Telegram Bot API (`python-telegram-bot`) |
| RSS parsing | `feedparser` |
| Scheduling | `APScheduler` or `schedule` |
| Env management | `python-dotenv` |

---

## 📦 Pydantic Models (`models/schemas.py`)

Define all models here. Use `model_config = ConfigDict(strict=True)` for all models.

```python
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from typing import Optional

class RawArticle(BaseModel):
    model_config = ConfigDict(strict=True)
    title: str
    link: str
    summary: str          # clean text: RSS excerpt or HTML-extracted fallback
    source: str           # e.g. "ynet", "israel_hayom", "the_marker", "geektime"
    published_at: datetime

class Interest(BaseModel):
    topic: str
    weight: float = Field(ge=0.0, le=1.0)
    sub_topics: list[str] = []

class BehaviorMemory(BaseModel):
    positive_signals: list[str] = []   # event_ids the user liked
    negative_signals: list[str] = []   # event_ids the user disliked

class PushManagement(BaseModel):
    daily_limit: int = 15
    current_count: int = 0
    dynamic_threshold: float = 8.0

class UserProfile(BaseModel):
    interests: list[Interest] = []
    avoid_topics: list[str] = []
    behavior_memory: BehaviorMemory = BehaviorMemory()
    push_management: PushManagement = PushManagement()

class ProcessedEvent(BaseModel):
    event_id: str                     # uuid4
    combined_summary: str             # 3-4 sentences in Hebrew
    relevance_score: float = Field(ge=1.0, le=10.0)
    reasoning: str                    # CoT output from Critic
    sources: list[RawArticle]
```

---

## 🤖 Agent Architecture

### Node 1 — The Scout (`agents/scout.py`)

**Responsibility:** Fetch and parse RSS feeds → return `list[RawArticle]`.

RSS feed URLs:
- Ynet: `https://www.ynet.co.il/Integration/StoryRss2.xml`
- Israel Hayom: `https://www.israelhayom.co.il/rss.xml`
- The Marker: `https://www.themarker.com/srv/tm-all-articles` (replaced Calcalist — their feed was malformed XML)
- Geektime: `https://www.geektime.co.il/feed/`

Implementation notes:
- Use `feedparser` for parsing.
- Parse `published_at` carefully — feeds use different date formats. Use `email.utils.parsedate_to_datetime` or `dateutil.parser.parse` as fallback.
- Return only articles published in the last 6 hours to avoid re-processing.
- Deduplicate by `link`.

#### Summary fallback (RSS → HTML)
- RSS summaries are HTML-stripped at parse time via `_strip_html()`.
- After all feeds are collected, `_fill_missing_summaries()` runs automatically — no opt-in flag.
- Any article whose stripped summary is shorter than `MIN_SUMMARY_LENGTH = 30` chars gets its summary replaced with the first 800 chars of text extracted from the article URL via `trafilatura`.
- Fallback fetches run in parallel (`ThreadPoolExecutor`, `ENRICH_WORKERS = 8`).
- If a fetch or extraction fails, the short summary is kept as-is — never raises.
- Constants: `FETCH_TIMEOUT = 10` seconds, `ENRICH_WORKERS = 8`, `MIN_SUMMARY_LENGTH = 30`.

---

### Node 2 — The Critic (`agents/critic.py`)

**Responsibility:** Cluster → Synthesize → Score → return `list[ProcessedEvent]`.

#### Step 1: Clustering
- Group `RawArticle` objects that discuss the same event.
- Strategy: Send all titles + sources to Claude and ask it to return cluster assignments as JSON.
- Use the following prompt pattern:

```xml
<task>
  You are a news clustering expert. Group the following Hebrew news headlines
  by the real-world event they describe. Return ONLY valid JSON.
</task>
<articles>
  {{articles_json}}
</articles>
<output_format>
  {"clusters": [{"event_id": "uuid", "article_indices": [0, 2, 5]}, ...]}
</output_format>
```

#### Step 2: Synthesis
- For each cluster, call Claude with all article summaries.
- **Prompt must use `<thinking>` tags for CoT reasoning** before outputting the final summary.
- Highlight diverging viewpoints between sources if present.
- Output: 3–4 sentences in Hebrew.

Synthesis prompt pattern:
```xml
<thinking>
  Analyze the articles, identify the core event, and note any differences
  in framing or emphasis between sources.
</thinking>
<task>
  Write a 3-4 sentence summary in Hebrew of the event described below.
  If sources present different perspectives, explicitly note the disagreement.
</task>
<articles>
  {{articles_json}}
</articles>
<user_profile>
  {{user_interests}}
</user_profile>
```

#### Step 3: Scoring
- Ask Claude to assign a `relevance_score` (1–10) based on the `UserProfile`.
- Include the `reasoning` field in the response.
- Parse response into `ProcessedEvent`.

**LLM call conventions** (`services/llm_client.py`):
- Always use `model="claude-sonnet-4-5"`.
- Set `max_tokens=2000` for synthesis calls, `max_tokens=500` for scoring.
- Wrap all API calls in try/except for `anthropic.APIError`.

---

### Node 3 — The Router (`agents/router.py`)

**Responsibility:** Apply dynamic thresholding and decide whether to push a notification.

```python
def should_send_push(event: ProcessedEvent, profile: UserProfile) -> bool:
    count = profile.push_management.current_count
    score = event.relevance_score

    if count >= 15:
        return False
    elif count >= 11:
        return score > 9.5
    elif count >= 6:
        return score > 9.0
    else:  # count < 6
        return score > 8.0
```

If `should_send_push` returns `False` (but count < 15), log the event to `daily_log.json` for the end-of-day digest.

---

### Node 4 — The Learner (`agents/learner.py`)

**Responsibility:** Handle Telegram callback queries (Like / Dislike) and update `UserProfile`.

Telegram inline keyboard buttons per notification:
- `👍 אהבתי` → positive signal
- `👎 לא רלוונטי` → negative signal

On callback received:
1. Load `user_profile.json` (use `FileLock` to prevent race conditions).
2. Add `event_id` to `behavior_memory.positive_signals` or `negative_signals`.
3. Adjust `weight` of matching `Interest` topics (+0.1 for like, -0.1 for dislike, clamped to [0,1]).
4. Atomically write back: write to `user_profile.tmp.json`, then `os.replace()`.

---

## 🔒 Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Never hardcode secrets. Always use `os.environ.get()` or `python-dotenv`.**

---

## ✅ Coding Conventions

1. **Type hints everywhere** — all function signatures must be fully typed.
2. **Pydantic for all data** — never use raw dicts for structured data.
3. **Logging** — use Python's `logging` module (not `print`). Set level to `INFO` in production, `DEBUG` in dev.
4. **Docstrings** — every public function/class gets a one-line docstring.
5. **Error handling** — wrap all external calls (RSS, API, Telegram) in try/except with meaningful log messages.
6. **Atomic file writes** — always write JSON via a temp file + `os.replace()`.
7. **No global state** — pass dependencies explicitly; use dependency injection where needed.
8. **Tests** — each agent module has a corresponding test file using `pytest`.

---

## 🗓️ Sprint Plan — 3 Days

### Day 1 — Scaffolding + Scout ✅
- [ ] Create the full directory structure above.
- [ ] Create `.env.example` with all required keys.
- [ ] Install all dependencies in `requirements.txt`.
- [ ] Implement `models/schemas.py` — all Pydantic models.
- [ ] Implement `agents/scout.py` — RSS fetching from all 4 feeds.
- [ ] Write `tests/test_scout.py` — mock feedparser, assert `RawArticle` output.
- [ ] Test: run `python -m agents.scout` and print fetched articles count.

### Day 2 — The Critic Agent 🔧
- [ ] Implement `services/llm_client.py` — Anthropic SDK wrapper with error handling.
- [ ] Implement `agents/critic.py`:
  - [ ] Step 1: Clustering via LLM.
  - [ ] Step 2: Synthesis with CoT + multi-perspective detection.
  - [ ] Step 3: Scoring against UserProfile.
- [ ] Write `tests/test_critic.py` — mock LLM calls, assert `ProcessedEvent` output.
- [ ] Tune prompts: test with real RSS data and inspect Hebrew summary quality.

### Day 3 — Telegram + Router + Learner 🚀
- [ ] Implement `services/telegram_bot.py` — send message with inline keyboard.
- [ ] Implement `agents/router.py` — dynamic threshold logic.
- [ ] Implement `agents/learner.py` — callback handler + atomic profile update.
- [ ] Implement `main.py` — wire all agents together, add scheduler (run every 30 min).
- [ ] Write `tests/test_router.py` — test all threshold boundary conditions.
- [ ] End-to-end test: run the full pipeline and receive a Telegram notification.

---

## 🧪 Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Run the full pipeline once
python main.py --run-once

# Start the scheduler (runs every 30 min)
python main.py --scheduler

# Run tests
pytest tests/ -v
```

---

## 📋 `requirements.txt`

```
anthropic>=0.25.0
pydantic>=2.0.0
feedparser>=6.0.0
python-telegram-bot>=21.0
python-dotenv>=1.0.0
apscheduler>=3.10.0
python-dateutil>=2.9.0
filelock>=3.13.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

## ⚠️ Important Notes for Claude Code

- **Start every session** by reading this file and the current state of `data/user_profile.json`.
- **Before implementing any agent**, check if `models/schemas.py` exists and matches the models above.
- **When modifying `UserProfile`**, always use atomic writes (temp file + `os.replace()`).
- **LLM calls in tests** must be mocked — never make real API calls in tests.
- **Hebrew text** — ensure all file operations use `encoding="utf-8"`.
- **The `event_id`** field in `ProcessedEvent` must be generated with `uuid.uuid4()`.
