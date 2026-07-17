"""
Pre-Market Research Desk — Backend
===================================

Local Flask server:
  1. Schwab: price, extended-hours bars, options chains
  2. Finnhub: macro calendar + mega-cap earnings
  3. Carmine-aligned S/D + premarket pack + strict playbook filter
  4. Actual option entry recommendations (weekly default; selective 0DTE)
  5. Tracker DB for empirical outcomes

Run:
    python app.py
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

from schwab_client import SchwabClient
from finnhub_client import FinnhubClient
from analysis import (
    analyze_instrument,
    rank_setups,
    current_desk_phase,
    now_et,
    now_pt,
    build_morning_plan,
    build_prompt_cowboy_board,
)
from tracker import OutcomeTracker

BASE_DIR = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = DATA_DIR / "tracker.db"


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your API credentials. See SETUP.md."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


config = load_config()

schwab = SchwabClient(
    app_key=config["schwab"]["app_key"],
    app_secret=config["schwab"]["app_secret"],
    callback_url=config["schwab"]["callback_url"],
    token_path=DATA_DIR / "schwab_tokens.json",
)

finnhub = FinnhubClient(api_key=config["finnhub"]["api_key"])
tracker = OutcomeTracker(db_path=DB_PATH)

import news_feed
news_feed.init(finnhub)


def safe_news_summary(max_headlines=3):
    """News is flag-only — a news failure must never break a scan."""
    try:
        return news_feed.build_news_summary(max_headlines=max_headlines)
    except Exception as e:
        app.logger.warning("news summary skipped: %s", e)
        return None

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/scan", methods=["GET"])
def scan():
    """
    Full morning pipeline. Works in premarket AND RTH session windows.
    Prints actual option entries when playbook + liquidity gates pass.
    """
    try:
        today = now_et().date()
        phase = current_desk_phase()

        if today.weekday() >= 5:
            return jsonify({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_closed": "Markets are closed on weekends.",
                "session_phase": "closed",
                "clock": {
                    "et": now_et().strftime("%Y-%m-%d %H:%M %Z"),
                    "pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
                },
                "macro_summary": {
                    "high_impact_events": [],
                    "all_events": [],
                    "earnings_today": [],
                },
                "instruments": [],
                "recommendations": [],
                "tracker_stats": tracker.get_rolling_stats(days=30),
            })

        # Finnhub free tier: economic calendar often 403 — never kill the scan
        try:
            macro_events = finnhub.economic_calendar(today, today) or []
        except Exception as e:
            app.logger.warning("macro calendar skipped: %s", e)
            macro_events = []
        try:
            earnings = finnhub.earnings_calendar(
                today,
                today,
                tickers=["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"],
            ) or []
        except Exception as e:
            app.logger.warning("earnings calendar skipped: %s", e)
            earnings = []

        results = []
        for symbol in ["SPY", "QQQ"]:
            # Multi-TF Schwab history (same candle family you see in TOS)
            bars_daily = None  # set below in try
            try:
                bars_daily = schwab.get_price_history(
                    symbol,
                    period_type="month",
                    period=6,
                    frequency_type="daily",
                    frequency=1,
                )
            except Exception as e:
                msg = str(e)
                if "401" in msg or "400" in msg or "token" in msg.lower() or "oauth" in msg.lower():
                    return jsonify({
                        "error": (
                            "Schwab login expired (refresh token is ~7 days). "
                            "In a terminal run:\n"
                            "  cd C:\\Users\\Welcome\\Documents\\VibeProjects\\premarket-trader\\backend\n"
                            "  python setup_auth.py\n"
                            "Paste the browser callback URL when prompted, then restart "
                            "python app.py and scan again. Detail: " + msg
                        )
                    }), 401
                raise

            bars_5m = schwab.get_price_history(
                symbol, period_type="day", period=5,
                frequency_type="minute", frequency=5,
            )
            bars_15m = schwab.get_price_history(
                symbol, period_type="day", period=10,
                frequency_type="minute", frequency=15,
            )
            # Schwab minute series max is usually 30m for "hour-like"; aggregate if needed
            bars_30m = schwab.get_price_history(
                symbol, period_type="day", period=10,
                frequency_type="minute", frequency=30,
            )
            if not bars_15m or len(bars_15m) < 20:
                bars_15m = bars_30m
            bars_1h = []  # built via aggregate in analysis from 15m/30m

            quote = schwab.get_quote(symbol)

            analysis = analyze_instrument(
                symbol=symbol,
                daily_bars=bars_daily,
                intraday_bars=bars_15m,
                quote=quote,
                macro_events=macro_events,
                earnings_events=earnings,
                bars_5m=bars_5m,
                bars_15m=bars_15m,
                bars_1h=bars_1h or bars_30m,
            )

            if analysis.get("setup"):
                strike_rec = select_option_contract(symbol, analysis["setup"])
                analysis["setup"]["strike_recommendation"] = strike_rec
                # If liquidity killed the contract in premarket, drop setup for print
                if strike_rec and strike_rec.get("rejected"):
                    analysis["setup"]["liquidity_rejected"] = True
                    analysis["setup"]["liquidity_reason"] = strike_rec.get("reason")
                    # Keep research view but do not recommend for size
                    if phase == "premarket":
                        # Hard reject in premkt if untradeable options
                        analysis["reject_log"] = analysis.get("reject_log", []) + [{
                            "reason": "option_liquidity",
                            "detail": strike_rec.get("reason"),
                        }]
                        analysis["setup_research"] = analysis["setup"]
                        analysis["setup"] = None
                elif strike_rec and not strike_rec.get("rejected"):
                    analysis["setup"]["option_entry"] = {
                        "symbol": strike_rec.get("symbol"),
                        "strike": strike_rec.get("strike"),
                        "expiration": strike_rec.get("expiration"),
                        "style": strike_rec.get("style"),
                        "side": analysis["setup"]["direction"],
                        "debit_mid": strike_rec.get("mid"),
                        "bid": strike_rec.get("bid"),
                        "ask": strike_rec.get("ask"),
                        "spread": strike_rec.get("spread"),
                        "spread_pct": strike_rec.get("spread_pct"),
                        "delta": strike_rec.get("delta"),
                        "volume": strike_rec.get("volume"),
                        "open_interest": strike_rec.get("open_interest"),
                        "session": analysis["setup"].get("session_label"),
                    }

            results.append(analysis)

        ranked = rank_setups(results, max_recs=2)
        recommendations = [
            r for r in ranked
            if r.get("setup") and r.get("recommended")
        ]

        tracker.check_outcomes(schwab)
        for r in recommendations:
            tracker.record_recommendation(r)

        stats = tracker.get_rolling_stats(days=30)
        macro_summary = summarize_macro(macro_events, earnings)
        morning_plan = build_morning_plan(
            ranked, recommendations, phase, macro_summary=macro_summary
        )
        prompt_cowboy = build_prompt_cowboy_board(morning_plan, phase=phase)

        return jsonify({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_phase": phase,
            "clock": {
                "et": now_et().strftime("%Y-%m-%d %H:%M %Z"),
                "pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
            },
            "entry_window_note": (
                "Premarket option entries enabled with liquidity gates. "
                "RTH open–11:30 ET (~6:30–8:30 AM PT) is the primary execution window. "
                "Weekly default; 0DTE only A-tier. "
                "Plan density always fills even with ZERO live entries."
            ),
            "macro_summary": macro_summary,
            "morning_plan": morning_plan,
            "prompt_cowboy": prompt_cowboy,
            "news_summary": safe_news_summary(max_headlines=3),
            "instruments": ranked,
            "recommendations": [
                {
                    "symbol": r["symbol"],
                    "setup": r["setup"],
                    "premarket_pack": r.get("premarket_pack"),
                    "trend": r.get("trend"),
                    "current_price": r.get("current_price"),
                }
                for r in recommendations
            ],
            "tracker_stats": stats,
        })

    except Exception as e:
        app.logger.exception("scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/news", methods=["GET"])
def news():
    """
    Market news + naive sentiment. FLAG-ONLY: never gates a trade.
    Cached 5 min (data/news_cache.json) so refreshes don't spam Finnhub.
    """
    summary = safe_news_summary(max_headlines=6)
    if summary is None:
        return jsonify({"error": "news unavailable", "news_summary": None}), 502
    return jsonify({"news_summary": summary})


@app.route("/api/brief", methods=["GET"])
def brief():
    """
    Discord-ready morning brief (markdown text).
    Uses same pipeline as /api/scan via morning_brief module.
    """
    try:
        from morning_brief import run_pipeline, format_discord_brief
        payload = run_pipeline()
        text = format_discord_brief(payload)
        return jsonify({
            "text": text,
            "session_phase": payload.get("phase"),
            "clock": payload.get("clock"),
            "headline": (payload.get("morning_plan") or {}).get("headline"),
        })
    except Exception as e:
        app.logger.exception("brief failed")
        return jsonify({"error": str(e), "text": f"**Premarket desk error:** {e}"}), 500


@app.route("/api/zone-alerts", methods=["GET"])
def zone_alerts_status():
    """Status of zone radar (does not spam Discord). Optional force dry-run message."""
    try:
        from zone_alerts import tick, load_state, current_desk_phase, now_pt
        # status_only uses analysis imports via tick
        # Call module tick with capture — redo light
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = tick(force=False, status_only=True)
        raw = buf.getvalue().strip()
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        st = load_state()
        return jsonify({
            "ok": code == 0,
            "phase": payload.get("phase") or current_desk_phase(),
            "status": payload,
            "state_path": str(DATA_DIR / "zone_alert_state.json"),
            "event_keys": list((st.get("events") or {}).keys())[:20],
            "clock_pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
            "note": "Alerts only fire in premarket / rth_session (open–11:30 ET) via cron.",
        })
    except Exception as e:
        app.logger.exception("zone-alerts status failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/start", methods=["GET"])
def auth_start():
    return jsonify({"url": schwab.get_auth_url()})


@app.route("/api/auth/callback", methods=["POST"])
def auth_callback():
    data = request.get_json()
    schwab.exchange_code(data["callback_url"])
    return jsonify({"status": "ok"})

@app.route("/api/history", methods=["GET"])
def history():
    """Return recent recommendations for history + journal view."""
    limit = min(int(request.args.get("limit", 50)), 100)
    days = min(int(request.args.get("days", 90)), 365)
    rows = tracker.list_journal(limit=limit, days=days)
    return jsonify(rows)


@app.route("/api/journal", methods=["GET", "POST"])
def journal():
    """
    GET  — journal rows + analytics
    POST — update decision/note/rating for one recommendation id
           body: {id, decision?, note?, rating?}
    """
    if request.method == "GET":
        limit = min(int(request.args.get("limit", 50)), 100)
        days = min(int(request.args.get("days", 60)), 365)
        return jsonify({
            "entries": tracker.list_journal(limit=limit, days=days),
            "analytics": tracker.journal_analytics(days=days),
            "decisions": [
                "took", "passed", "fomo", "late", "managed", "skipped_macro", "other"
            ],
            "tip": (
                "Tag each rec: took/passed/fomo/late. Win% printed only from closed "
                "price outcomes — not conviction scores."
            ),
        })

    data = request.get_json(force=True, silent=True) or {}
    rec_id = data.get("id")
    if not rec_id:
        return jsonify({"error": "id required"}), 400
    result = tracker.update_journal(
        rec_id=int(rec_id),
        decision=data.get("decision"),
        note=data.get("note"),
        rating=data.get("rating"),
    )
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/journal/stats", methods=["GET"])
def journal_stats():
    days = min(int(request.args.get("days", 60)), 365)
    return jsonify(tracker.journal_analytics(days=days))


@app.route("/api/health", methods=["GET"])
def health():
    finnhub_ok = False
    try:
        today = now_et().date()
        finnhub.economic_calendar(today, today)
        finnhub_ok = True
    except Exception:
        pass
    return jsonify({
        "schwab_authed": schwab.is_authenticated(),
        "finnhub_key_valid": finnhub_ok,
        "tracker_db": str(DB_PATH),
        "session_phase": current_desk_phase(),
        "clock_pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
    })


# ---------------------------------------------------------------
# Options selection with liquidity gates (premarket-aware)
# ---------------------------------------------------------------

def select_option_contract(symbol, setup):
    """
    Pick weekly (default) or 0DTE (A-tier only) contract near ~0.40 |delta|.
    Liquidity gates:
      - bid & ask present and ask > 0
      - spread/mid <= 15% (tighter 10% if premkt)
      - prefer non-zero volume or OI when available
    """
    direction = setup["direction"]
    phase = setup.get("session_phase") or current_desk_phase()
    allow_0dte = setup.get("allow_0dte") and setup.get("zone_fresh")

    # Try 0DTE first only if A-tier and session warrants (RTH preferred, premkt strict)
    ordered_styles = []
    if allow_0dte:
        ordered_styles.append("0dte")
    ordered_styles.append("weekly")

    last_reject = None
    for style in ordered_styles:
        if style == "0dte":
            exp = setup.get("target_expiry_0dte") or now_et().date().isoformat()
            from_d, to_d = exp, exp
        else:
            exp = setup.get("target_expiry")
            # Broader weekly window in case of holiday expiry
            from_d = exp
            to_d = (
                datetime.fromisoformat(exp).date() + timedelta(days=4)
            ).isoformat() if exp else None

        chain = schwab.get_option_chain(
            symbol=symbol,
            contract_type=direction,
            from_date=from_d,
            to_date=to_d,
        )
        if not chain.get("contracts"):
            # broaden
            today_str = now_et().date().isoformat()
            end_str = (now_et().date() + timedelta(days=10)).isoformat()
            chain = schwab.get_option_chain(
                symbol=symbol,
                contract_type=direction,
                from_date=today_str,
                to_date=end_str,
            )

        picked = pick_strike(chain, setup, style=style, phase=phase)
        if picked and not picked.get("rejected"):
            picked["style"] = style
            return picked
        if picked:
            last_reject = picked

    return last_reject or {
        "rejected": True,
        "reason": "no_contracts",
    }


def pick_strike(chain, setup, style="weekly", phase="rth_session"):
    if not chain or "contracts" not in chain or not chain["contracts"]:
        return {"rejected": True, "reason": "empty_chain"}

    target_delta = 0.40 if setup["direction"] == "CALL" else -0.40
    max_spread_pct = 0.10 if phase == "premarket" else 0.15
    min_mid = 0.15  # avoid worthless dust

    ranked = []
    for c in chain["contracts"]:
        if c.get("delta") is None:
            continue
        bid = c.get("bid")
        ask = c.get("ask")
        if bid is None or ask is None or ask <= 0:
            continue
        if ask < bid:
            continue
        mid = (bid + ask) / 2.0
        if mid < min_mid:
            continue
        spread = ask - bid
        spread_pct = spread / mid if mid else 999
        if spread_pct > max_spread_pct:
            continue
        # Soft liquidity score
        vol = c.get("volume") or 0
        oi = c.get("open_interest") or 0
        liq = vol + oi * 0.25
        dist = abs(c["delta"] - target_delta)
        ranked.append((dist, -liq, spread_pct, {**c, "mid": round(mid, 2), "spread": round(spread, 2), "spread_pct": round(spread_pct, 3)}))

    if not ranked:
        return {
            "rejected": True,
            "reason": f"no_contract_passed_liquidity (max_spread_pct={max_spread_pct})",
            "style": style,
        }

    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    best = ranked[0][3]
    best["rejected"] = False
    best["style"] = style
    return best


def summarize_macro(events, earnings):
    high_impact = [e for e in events if e.get("impact") == "high"]
    notes = ["Hostile macro is a FLAG only — setups are not auto-killed."]
    if not events and not earnings:
        notes.append(
            "No macro/earnings feed this run (Finnhub free tier often blocks "
            "/calendar/economic). SPY/QQQ analysis still ran."
        )
    elif not events:
        notes.append(
            "Economic calendar empty or blocked on free Finnhub; earnings feed used if available."
        )
    return {
        "high_impact_events": high_impact,
        "all_events": events or [],
        "earnings_today": earnings or [],
        "note": " ".join(notes),
    }


if __name__ == "__main__":
    print("=" * 60)
    print("Pre-Market Research Desk  ·  Carmine session build")
    print("=" * 60)
    print(f"Schwab authenticated: {schwab.is_authenticated()}")
    print(f"Session phase:        {current_desk_phase()}")
    print(f"Clock PT:             {now_pt().strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Frontend directory:   {FRONTEND_DIR}")
    print(f"Tracker database:     {DB_PATH}")
    print("=" * 60)
    print("Open http://127.0.0.1:5000 in your browser.")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False)
