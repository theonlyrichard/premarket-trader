# HANDOVER — Pre-Market Research Desk (premarket-trader)

**Date:** 2026-07-14
**Owner:** Richard Cardoza (richard.cardoza16@gmail.com)
**Repo state:** branch `master`, in sync with `origin/master`, 4 commits, working tree clean except one untracked screenshot (`Screenshot 2026-05-02 201639.png` — not needed, safe to delete or ignore).

---

## 1. What this is

A **local, single-user Flask dashboard** that runs each morning before the US market opens. It:

1. Pulls pre-market price data (bars, quotes, options chains) for **SPY and QQQ** from the **Schwab Trader API**.
2. Pulls the day's **economic calendar and mega-cap earnings** from **Finnhub** (free tier).
3. Runs a deterministic **supply/demand zone + volume + confluence** analysis pipeline.
4. Produces **0–2 trade recommendations** (entry trigger, stop, target, R:R, suggested ~0.40-delta options contract) ranked by a 0–10 factor-convergence "conviction" score.
5. **Silently logs every recommendation** to a local SQLite DB and later resolves each one to win/loss/expired using subsequent price data, so the dashboard shows an *empirical* rolling 30-day win rate per conviction tier.

Design philosophy (important to preserve): **no fabricated probabilities**. Conviction scores are factor counts, not win probabilities. The only "does this work?" number is the measured win rate from the tracker. "No trade today" is an intended, common outcome (min R:R 1.5 gate) — the user wants ~1–2 trades per week, not daily signals.

Everything runs on the user's Windows machine, binds to `127.0.0.1:5000` only, zero paid services.

## 2. Current status (as of handover)

- **2026-07-14 P0+P1 upgrade shipped:** Carmine-aligned multi-TF/playbook zones (RBD/DBR/compression), **premarket levels pack**, session phases (premarket actual option entries + RTH), weekly options default / selective 0DTE A-tier, **liquidity gates** on contracts, hostile macro = **flag only** (no −3 conviction kill). Frontend renamed Playbook Desk with session + levels + option entry cards.
- **App is functional** after earlier bug-fix rounds + this playbook rewrite. Analysis unit smoke-test passes; `/api/health` loads.
- **`data/tracker.db` still needs live morning scans** to fill. Empirical loop unproven on real data.
- Local secrets: `config.json`, `data/schwab_tokens.json` (gitignored). **`schwab_authed: True` only means a refresh token file exists** — token is almost certainly stale since May. Re-run `python setup_auth.py` before live scan. Finnhub key was `finnhub_key_valid: False` on last health check — verify key in config.json.
- **Next ops:** (1) reauth Schwab (2) morning premarket scan on a market day (3) Discord auto-brief / live zone alerts = P2/P3 still open.

## 3. How to run it

Full first-time setup (Schwab developer app registration, OAuth, etc.) is documented step-by-step in **`SETUP.md`** — it is accurate, keep it in sync with any changes. Day-to-day:

```
cd premarket-trader
venv\Scripts\activate          # Windows; deps: flask>=3.0, requests>=2.31 (that's all)
cd backend
python setup_auth.py           # only if refresh token expired (>7 days)
python app.py                  # serves http://127.0.0.1:5000
```

Open the URL, click **Run Morning Scan**. Best run pre-market on a trading day; weekends return a "market closed" payload, holidays may return "Insufficient data".

## 4. File map

```
config.json               # REAL secrets (gitignored). Schema in config.example.json.
config.example.json       # template: schwab.app_key/app_secret/callback_url, finnhub.api_key
requirements.txt          # flask, requests — nothing else
SETUP.md                  # complete end-user setup guide (user-facing tone)
data/                     # gitignored
  tracker.db              # SQLite outcome tracker (currently empty)
  schwab_tokens.json      # OAuth tokens; delete + re-run setup_auth.py to reset
backend/
  app.py                  # Flask server + routes + strike picker (~260 lines)
  analysis.py             # entire analysis pipeline, pure functions (~500 lines)
  schwab_client.py        # OAuth + market-data wrapper for Schwab
  finnhub_client.py       # economic calendar, earnings, news
  tracker.py              # OutcomeTracker (SQLite): record / resolve / stats
  setup_auth.py           # one-time interactive OAuth bootstrap
frontend/
  index.html              # ENTIRE frontend: single file, vanilla HTML/CSS/JS, no build step
```

## 5. Backend architecture

### HTTP API (`backend/app.py`)

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | serves `frontend/index.html` |
| `/api/scan` | GET | the whole pipeline (see below); returns macro summary, ranked instruments, tracker stats |
| `/api/history?limit=N` | GET | last N (≤100) recommendations from SQLite for the history view |
| `/api/health` | GET | `schwab_authed` (refresh token exists — NOT validity), `finnhub_key_valid` (live call), db path |
| `/api/auth/start` | GET | returns Schwab authorize URL |
| `/api/auth/callback` | POST | body `{callback_url}` → exchanges code for tokens |

### `/api/scan` sequence

1. Weekend guard (UTC weekday ≥ 5 → early return with empty payload + stats).
2. Finnhub: US economic calendar for today + earnings filtered to AAPL/MSFT/NVDA/AMZN/META/GOOGL/TSLA.
3. Per symbol (SPY, QQQ): Schwab daily bars (1 month), 30-min bars (5 days, extended hours), quote → `analyze_instrument()`.
4. If a setup was found: fetch options chain for the computed target expiry (next Friday); **if empty (holiday expiry), fall back to a today+7-days window**; pick the contract nearest ±0.40 delta (`pick_strike`).
5. `rank_setups()` sorts instruments by conviction score.
6. `tracker.check_outcomes(schwab)` — resolve old open recommendations **before** recording new ones.
7. `tracker.record_recommendation()` for each ranked setup (deduped per symbol+direction+zone+day).
8. Return JSON. Any exception → logged, `{"error": ...}` 500 (frontend displays it).

### Analysis pipeline (`backend/analysis.py` — all pure/deterministic)

- **`detect_zones`**: scans last 100 intraday bars for an "explosive" candle (range > 1.5× avg AND body > 60% of range) preceded by a 1–3 bar tight base (base avg range < 0.9× avg). Zone = base high/low. Demand if explosive candle closed up, supply if down. `fresh` = price never re-entered the zone after origin.
- **`find_nearest_zones`**: nearest *fresh* demand below price / *fresh* supply above.
- **`detect_volume_spikes`**: avg volume over 100 bars; flags recent bars >1.8×; per-zone origin volume ratio (confirmation if >1.3×). ⚠️ keyed by `id(zone)` — Python object identity, works only within the single request; don't serialize or reorder.
- **`confluence_check`**: counts alignment of zone-mid (tolerance 1.5× zone height) with prior-week H/L/C, daily EMA 20/50/200, session VWAP (approximated over last 20 intraday bars), round-$5 numbers.
- **`score_macro`**: hostile if any high-impact US event falls within the next 8h hold window or any tracked mega-cap reports today.
- **`build_setup`**: demand→CALL / supply→PUT. Entry = 1-min close beyond zone edge after a tag; stop = zone edge ± 30% of zone height; target = prior-week high/low (fallback 2.5R). **Rejects if R:R < 1.5 (returns None).** Expiry = next Friday.
- **`score_conviction`** (0–10): fresh zone +2; zone strength ≥2×/≥1.5× +2/+1; origin volume ≥1.8×/≥1.3× +2/+1; confluence ≥3/≥1 +2/+1; R:R ≥2.5 +1; hostile macro **−3**. Labels: High ≥8, Medium 5–7, Low <5.

### Outcome tracker (`backend/tracker.py`)

- One table `recommendations` (schema in `_init_db`; full setup JSON kept in `raw_setup`).
- `record_recommendation`: dedupes on symbol + direction + zone (±0.5) + same UTC date. `entry_trigger_price` = zone top (CALL) or bottom (PUT).
- `check_outcomes`: for each `open` rec recorded before today, pulls **5 days of 5-min bars** and walks bars after `recorded_at`: CALL → win if `high ≥ target` before `low ≤ stop` (PUT mirrored; note a bar hitting both counts as win — known simplification). Unresolved after **>5 days → `expired`**. Per-row failures print and continue; they never break a scan.
- `get_rolling_stats(days=30)`: totals + per-conviction-label W/L and win rate; `win_rate` is None until ≥1 closed trade. UI shows "Collecting data…" until then; ~20–30 closed trades needed for significance.

### API clients

- **`schwab_client.py`**: OAuth2 (Basic auth header from app_key:app_secret). Access token auto-refreshes 60s before its `expires_in` (default 1800s), using `saved_at` stamped at save time. Refresh merges new tokens over old (Schwab often omits refresh_token in refresh responses). Endpoints wrapped: `/marketdata/v1/{symbol}/quotes`, `/pricehistory` (extended hours on), `/chains` (flattened from Schwab's nested `callExpDateMap`/`putExpDateMap` to a simple contract list with greeks).
- **`finnhub_client.py`**: economic calendar (filtered `country == "US"`), earnings calendar (optional ticker filter), `market_news` (implemented, **currently unused** — candidate feature).

## 6. Frontend (`frontend/index.html`)

One self-contained file, no framework, no build. Key JS functions: `runScan()` (fetches `/api/scan`, guards against repeated clicks — the `loadingBox` null bug fixed in `4b1c131`), `renderTracker(stats)` (win-rate bar with per-conviction sample sizes), `renderMacro`, `renderRecommendations`/`renderRecCard` (setup cards: direction, entry trigger, stop/target/R:R, conviction breakdown, strike recommendation), `toggleHistory()`/`loadHistory()` (fetches `/api/history`), `buildHeadline(setup)`. If you change any API response shape, this file is the only consumer.

## 7. Gotchas, quirks, known limitations

1. **Schwab refresh token expires every 7 days** — the #1 recurring failure. Symptoms: 401s during scan, or `finnhub ok / schwab 401`. Fix is always `python setup_auth.py`. `is_authenticated()` / startup banner will *still say True* because it only checks token existence.
2. **Schwab app approval**: callback URL must be exactly `https://127.0.0.1` (no slash/port) and must match `config.json`.
3. **`id(zone)` volume map** (analysis.py): request-scoped object-identity keys. Refactor carefully if you ever cache or serialize zones.
4. **Mixed timezone idioms**: pipeline is mostly UTC (`datetime.now(timezone.utc)`), but `build_setup` computes expiry from local `datetime.now().date()`. Harmless on this machine (US user) but be careful when touching date logic — timezone bugs were already fixed once (commit `f114fb8`).
5. **Outcome window vs. bar window**: `check_outcomes` fetches only 5 days of bars; a rec unresolved in that window becomes `expired` (>5 days). Recs can silently expire over long gaps between scans (outcomes only update when a scan runs — there is no scheduler/cron).
6. **Win/loss tie-break**: within a single 5-min bar that spans both stop and target, win is checked first. Slight optimistic bias; acceptable for now, flagged for Phase 3.
7. **VWAP is approximate**: last 20 intraday 30-min bars, not a true session VWAP.
8. **`build_setup` receives `macro_info=None`** — macro is scored *after* setup construction and only affects conviction, not setup validity. Intentional.
9. **No tests, no CI, no linting.** Verification so far has been manual (run scan, eyeball dashboard). `data/` and `config.json` are gitignored; never commit them.
10. **Screenshot file** in repo root is untracked leftover; ignore or delete.

## 8. Suggested next steps (in priority order)

1. Re-auth Schwab (`setup_auth.py`) and run a live pre-market scan on a trading day; verify a recommendation lands in `tracker.db` and resolves on a later scan. The tracking loop has never processed real data.
2. Consider a scheduled/automatic morning scan (Windows Task Scheduler hitting `/api/scan`, or an in-app scheduler) so outcome resolution doesn't depend on the user remembering to click.
3. Once ~20–30 closed trades exist: Phase 3 — factor-vs-outcome analysis on `tracker.db` (which conviction factors actually predict wins), then tune `score_conviction` weights.
4. Optional: surface `finnhub.market_news()` (already implemented, unused) as an overnight-headlines panel.

## 9. Contract with the user (tone & expectations)

Richard is a retail trader, not a developer. Communication style used throughout (SETUP.md especially): plain language, no jargon without explanation, honest about uncertainty. Two standing commitments: (a) never present conviction scores as probabilities, (b) help analyze tracker.db once enough history exists. The app deliberately errs toward "no trade" — do not "improve" it by loosening the R:R ≥ 1.5 or freshness gates without data from the tracker to justify it.
