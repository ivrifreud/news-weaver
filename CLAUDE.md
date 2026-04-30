# CLAUDE.md — The News Weaver (Agentic News Aggregator)

> This file is read automatically by Claude Code at the start of every session.
> It contains the full project context, architecture, coding conventions, and current state.

---

## 🎯 Project Goal

Python-based intelligent news aggregator that:
- Fetches news from 8 RSS sources across 3 tiers (Israeli balance, global depth, tech/economy)
- Clusters related stories into unified events using Claude Haiku
- Generates multi-perspective Hebrew summaries + relevance scores via Claude Sonnet (single merged call)
- Sends personalized push notifications (max 15/day) via Telegram with like/dislike/expand buttons
- Learns from feedback: adjusts interest weights, persists queue across restarts
- Runs once daily at 07:00 UTC via APScheduler cron

---

## 🗂️ Repository Structure

```
news-weaver/
├── CLAUDE.md                  ← you are here
├── .env                       ← secrets (never commit)
├── .gitignore
├── requirements.txt
│
├── main.py                    ← entrypoint / scheduler
│
├── models/
│   └── schemas.py             ← all Pydantic models
│
├── agents/
│   ├── scout.py               ← Node 1: RSS ingestion
│   ├── critic.py              ← Node 2: clustering + synthesis + scoring
│   ├── router.py              ← Node 3: dynamic thresholding
│   └── learner.py             ← Node 4: feedback loop + expand
│
├── services/
│   ├── telegram_bot.py        ← Telegram Bot API wrapper
│   └── llm_client.py          ← Anthropic SDK wrapper
│
├── data/                      ← runtime state (gitignored)
│   ├── user_profile.json      ← persisted UserProfile
│   ├── events_cache.json      ← event summaries + source URLs for expand/feedback
│   └── feedback_queue.json    ← pending feedback items (drained at pipeline start)
│
├── tests/
│   ├── test_scout.py
│   ├── test_critic.py
│   └── test_router.py
│
├── eval_critic.py             ← offline prompt evaluation (3 fixed clusters)
└── inspect_articles.py        ← live feed inspection helper
```

---

## 🧱 Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.8+ |
| LLM (synthesis+score) | `claude-sonnet-4-5` via `anthropic` SDK |
| LLM (clustering) | `claude-haiku-4-5-20251001` (cheaper, fast) |
| Data validation | Pydantic v2, `ConfigDict(strict=True)` |
| Storage | Local JSON files |
| Messaging | Telegram Bot API (`python-telegram-bot` v21) |
| RSS parsing | `feedparser` + `trafilatura` (HTML fallback) |
| Scheduling | `APScheduler` (cron, daily 07:00 UTC) |
| Env management | `python-dotenv` |
| Concurrency | `threading` + `asyncio` (bot in daemon thread) |

---

## 📦 Pydantic Models (`models/schemas.py`)

All models use `model_config = ConfigDict(strict=True)`.

```python
class RawArticle(BaseModel):
    title: str
    link: str
    summary: str           # clean text: RSS excerpt or HTML-extracted fallback
    source: str            # e.g. "ynet", "bbc_world", "the_marker"
    published_at: datetime
    position_in_feed: int = 0  # 1-based rank; 1 = top headline

class Interest(BaseModel):
    topic: str
    weight: float = Field(ge=0.0, le=1.0)
    sub_topics: List[str] = []

class BehaviorMemory(BaseModel):
    positive_signals: List[str] = []   # event_ids the user liked
    negative_signals: List[str] = []   # event_ids the user disliked

class PushManagement(BaseModel):
    daily_limit: int = 15
    current_count: int = 0
    dynamic_threshold: float = 8.0
    last_reset_date: Optional[str] = None  # ISO date YYYY-MM-DD, UTC

class UserProfile(BaseModel):
    interests: List[Interest] = []
    avoid_topics: List[str] = []
    behavior_memory: BehaviorMemory = BehaviorMemory()
    push_management: PushManagement = PushManagement()

class ProcessedEvent(BaseModel):
    event_id: str              # uuid4
    combined_summary: str      # 3-4 sentences in Hebrew
    relevance_score: float = Field(ge=1.0, le=10.0)
    reasoning: str             # CoT thinking + score reasoning
    sources: List[RawArticle]
```

---

## 🤖 Agent Architecture

### Node 1 — The Scout (`agents/scout.py`)

**Responsibility:** Fetch and parse 8 RSS feeds → return `list[RawArticle]`.

#### RSS sources and per-run quotas

| Key | Quota | Language | URL |
|---|---|---|---|
| `ynet` | 5 | Hebrew | `https://www.ynet.co.il/Integration/StoryRss2.xml` |
| `israel_hayom` | 5 | Hebrew | `https://www.israelhayom.co.il/rss.xml` |
| `bbc_world` | 5 | English | `https://feeds.bbci.co.uk/news/world/rss.xml` |
| `the_economist` | 3 | English | `https://www.economist.com/latest/rss.xml` |
| `foreign_affairs` | 2 | English | `https://www.foreignaffairs.com/rss.xml` |
| `the_marker` | 4 | Hebrew | `https://www.themarker.com/srv/tm-all-articles` |
| `geektime` | 3 | Hebrew | `https://www.geektime.co.il/feed/` |
| `mit_tech_review` | 2 | English | `https://www.technologyreview.com/feed/` |

`MAX_AGE_HOURS = 24` — articles older than 24 hours are filtered out.

#### Summary fallback (RSS → HTML)
- Summaries are HTML-stripped at parse time via `_strip_html()`.
- `_fill_missing_summaries()` runs after all feeds: articles with summaries shorter than `MIN_SUMMARY_LENGTH = 30` chars get their summary replaced with the first 800 chars extracted via `trafilatura`.
- Parallel fetches: `ThreadPoolExecutor(max_workers=8)`, `FETCH_TIMEOUT = 10s`.
- Failures keep the short summary as-is — never raises.

---

### Node 2 — The Critic (`agents/critic.py`)

**Responsibility:** Cluster → Synthesize + Score in one merged call → return `list[ProcessedEvent]`.

#### Step 1: Clustering (Haiku)
- Sends all titles + sources + `position_in_feed` to `claude-haiku-4-5-20251001`.
- Returns JSON cluster assignments: `{"clusters": [{"event_id": "...", "article_indices": [0,2,5]}, ...]}`.
- Unassigned articles become singleton clusters. Falls back to all-singletons on parse error.
- `max_tokens=600`.

#### Step 2+3: Merged synthesis + score (Sonnet, single call per cluster)
- One call to `claude-sonnet-4-5` per cluster — no separate scoring call.
- Response format:
  ```
  <thinking>[CoT analysis]</thinking>
  [3-4 sentence Hebrew summary]
  <score>{"relevance_score": 7.5, "reasoning": "..."}</score>
  ```
- Prompt instructs: articles may be Hebrew or English — summary must always be Hebrew.
- When `ynet` and `israel_hayom` cover the same event, explicitly note framing differences.
- `position_in_feed` is passed via `<prominence>` block (not per-article row) to avoid prompt bloat.
- `max_tokens=2500`.

**LLM client** (`services/llm_client.py`):
- `max_retries=3` on the Anthropic client (handles 529 overload with backoff).
- `model` kwarg on `client.call()` — omit for Sonnet (default), pass `HAIKU_MODEL` for clustering.
- Usage tracking via `get_usage()` / `reset_usage()`.

---

### Node 3 — The Router (`agents/router.py`)

**Responsibility:** Dynamic thresholding — decide whether to push a notification.

```python
def should_send_push(event: ProcessedEvent, profile: UserProfile) -> bool:
    count = profile.push_management.current_count
    score = event.relevance_score

    if count >= 15:   return False
    elif count >= 11: return score > 9.5
    elif count >= 6:  return score > 9.0
    else:             return score > 8.0   # count < 6
```

Events below threshold (but count < 15) are logged to `data/daily_log.json`.

---

### Node 4 — The Learner (`agents/learner.py`)

**Responsibility:** Telegram callback handling, feedback queue, expand feature.

#### Telegram buttons (per notification)
- `👍 אהבתי` / `👎 לא רלוונטי` — like / dislike
- `🔍 הרחב` — fetch full article content and generate an 8-10 sentence deep-dive summary

#### Feedback queue (persistent)
- Callbacks write to `data/feedback_queue.json` (atomic append) instead of directly modifying the profile.
- At the start of every pipeline run, `process_feedback_queue()` drains the queue:
  - Loads profile with `FileLock`
  - Adjusts matching `Interest` weights (+0.1 like / -0.1 dislike, clamped [0,1])
  - Clears the queue atomically
- Survives bot restarts and pipeline downtime.

#### Expand
- Fetches full article HTML via `trafilatura` for each source URL in `events_cache`.
- Calls Sonnet with `_build_expand_prompt()` for an 8-10 sentence Hebrew deep-dive (`max_tokens=1500`).
- Auto-enqueues a "like" after expand (signals strong interest).

---

## ⏰ Scheduling (`main.py`)

- **`--run-once`**: runs pipeline once and exits.
- **`--scheduler`**: APScheduler cron job at `hour=7, minute=0` UTC. Also runs immediately on start.
- At the start of each pipeline run, `_reset_daily_count_if_needed()` checks `last_reset_date` against today (UTC) and resets `current_count` to 0 if it's a new day.
- Telegram bot runs in a daemon thread (`asyncio.new_event_loop()` + PTB Application built inside the thread to avoid Python 3.8 Future-loop coupling issues).

---

## 🔒 Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Never hardcode secrets. Always use `os.environ.get()` or `python-dotenv`.

---

## ✅ Coding Conventions

1. **Type hints everywhere** — all function signatures must be fully typed.
2. **Pydantic for all data** — never use raw dicts for structured data.
3. **Logging** — use `logging` (not `print`). `INFO` in production, `DEBUG` in dev.
4. **Docstrings** — every public function/class gets a one-line docstring.
5. **Error handling** — wrap all external calls (RSS, API, Telegram) in try/except with meaningful log messages.
6. **Atomic file writes** — always write JSON via a temp file + `os.replace()`.
7. **No global state** — pass dependencies explicitly.
8. **Tests** — each agent module has a corresponding test file using `pytest`. LLM calls must be mocked.

---

## 🧪 Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Run the full pipeline once (dry-run without TELEGRAM_BOT_TOKEN)
python main.py --run-once

# Start the daily scheduler (runs at 07:00 UTC, also runs immediately on start)
python main.py --scheduler

# Run tests
pytest tests/ -v

# Live feed inspection
python inspect_articles.py

# Offline critic eval (no real API calls needed if mocked)
python eval_critic.py
```

---

## ⚠️ Important Notes for Claude Code

- **`data/` is gitignored** — all runtime JSON is regenerated on first run.
- **`last_reset_date`** in `PushManagement` is an `Optional[str]` ISO date (UTC). Existing profiles without it deserialize correctly (defaults to `None`).
- **Bot thread**: `build_application(token)` must be called *inside* the async `_run_app()` coroutine in the bot thread — not in the main thread. This is a Python 3.8 asyncio constraint.
- **LLM calls in tests** must be mocked — never make real API calls in tests.
- **Hebrew text** — all file I/O uses `encoding="utf-8"`.
- **`event_id`** in `ProcessedEvent` is generated with `uuid.uuid4()`.
- **`position_in_feed`** is 1-based (1 = top headline); 0 means unknown.
