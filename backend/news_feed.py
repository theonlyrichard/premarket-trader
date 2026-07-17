"""
News feed — Finnhub headlines + naive keyword sentiment.

FLAG-ONLY: news never gates, overrides, or prints a trade. It is context
for the human, same policy as hostile macro.

Design notes:
  - Reuses FinnhubClient (call init(client) once at startup) so key handling
    and 401/403 degradation stay in one place.
  - General market news is the PRIMARY source. /company-news is nearly
    always empty for ETFs like SPY/QQQ, so it's merged in opportunistically
    and an empty result is normal.
  - File cache (data/news_cache.json) with per-key TTL so back-to-back
    scans don't spam the API (free tier: 60 calls/min).
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "news_cache.json"

_finnhub = None


def init(finnhub_client):
    """Wire in the shared FinnhubClient instance (call once from app.py)."""
    global _finnhub
    _finnhub = finnhub_client


# ── Naive sentiment ──────────────────────────────────────────────────

BULLISH_WORDS = [
    "rally", "rallies", "surge", "surges", "soar", "soars", "jump", "jumps",
    "gain", "gains", "climb", "climbs", "rise", "rises", "rebound", "rebounds",
    "beat", "beats", "record high", "all-time high", "upgrade", "upgraded",
    "bullish", "optimism", "optimistic", "strong", "strength", "boost",
    "boosts", "breakout", "buyback", "outperform", "tops estimates", "growth",
]

BEARISH_WORDS = [
    "fall", "falls", "drop", "drops", "plunge", "plunges", "slump", "slumps",
    "tumble", "tumbles", "sink", "sinks", "slide", "slides", "decline",
    "declines", "selloff", "sell-off", "crash", "crashes", "miss", "misses",
    "downgrade", "downgraded", "bearish", "fear", "fears", "recession",
    "layoff", "layoffs", "warning", "warns", "weak", "weakness", "losses",
    "job cuts", "cuts guidance", "cuts forecast", "cuts outlook",
    "tariff", "tariffs", "inflation jumps", "default", "shutdown",
]

_BULL_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BULLISH_WORDS) + r")\b", re.I)
_BEAR_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BEARISH_WORDS) + r")\b", re.I)


def score_sentiment(text):
    """BULLISH / BEARISH / NEUTRAL from keyword counts. Naive by design."""
    if not text:
        return "NEUTRAL"
    bull = len(_BULL_RE.findall(text))
    bear = len(_BEAR_RE.findall(text))
    if bull > bear:
        return "BULLISH"
    if bear > bull:
        return "BEARISH"
    return "NEUTRAL"


# ── File cache with TTL ──────────────────────────────────────────────

def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_get(key, ttl_seconds):
    entry = _load_cache().get(key)
    if not entry:
        return None
    if time.time() - entry.get("fetched_at", 0) > ttl_seconds:
        return None
    return entry.get("data")


def _cache_put(key, data):
    cache = _load_cache()
    cache[key] = {"fetched_at": time.time(), "data": data}
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except OSError as e:
        log.warning("news cache write failed: %s", e)


# ── Fetchers ─────────────────────────────────────────────────────────

def get_finnhub_news(category="general", cache_seconds=300):
    """General market headlines, cached."""
    key = f"general:{category}"
    cached = _cache_get(key, cache_seconds)
    if cached is not None:
        return cached
    if _finnhub is None:
        return []
    items = _finnhub.market_news(category=category) or []
    _cache_put(key, items)
    return items


def get_symbol_news(symbol, days_back=1, cache_seconds=300):
    """Company headlines for one symbol, cached. Empty is normal for ETFs."""
    key = f"symbol:{symbol}:{days_back}"
    cached = _cache_get(key, cache_seconds)
    if cached is not None:
        return cached
    if _finnhub is None:
        return []
    today = datetime.now(timezone.utc).date()
    items = _finnhub.company_news(symbol, today - timedelta(days=days_back), today) or []
    _cache_put(key, items)
    return items


# ── Summary builder ──────────────────────────────────────────────────

def build_news_summary(symbols=("SPY", "QQQ"), max_headlines=6):
    """
    Returns:
      {headlines: [{datetime, source, headline, url, sentiment}],
       tally: {BULLISH: n, BEARISH: n, NEUTRAL: n},
       dominant: 'BULLISH'|'BEARISH'|'NEUTRAL',
       timestamp}
    Never raises — on total failure returns an empty-but-valid summary.
    """
    raw = []
    try:
        raw.extend(get_finnhub_news())
        for sym in symbols:
            raw.extend(get_symbol_news(sym))
    except Exception as e:
        log.warning("news fetch failed (flag-only, continuing): %s", e)

    seen = set()
    headlines = []
    for item in sorted(raw, key=lambda x: x.get("datetime", 0), reverse=True):
        title = (item.get("headline") or "").strip()
        if not title:
            continue
        dedup_key = title.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        headlines.append({
            "datetime": item.get("datetime"),  # epoch seconds
            "source": item.get("source") or "",
            "headline": title,
            "url": item.get("url") or "",
            "sentiment": score_sentiment(title + " " + (item.get("summary") or "")),
        })
        if len(headlines) >= max_headlines:
            break

    tally = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    for h in headlines:
        tally[h["sentiment"]] += 1

    dominant = "NEUTRAL"
    if tally["BULLISH"] > tally["BEARISH"]:
        dominant = "BULLISH"
    elif tally["BEARISH"] > tally["BULLISH"]:
        dominant = "BEARISH"

    return {
        "headlines": headlines,
        "tally": tally,
        "dominant": dominant,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "News is flag-only. It never gates, overrides, or prints a trade.",
    }
