"""
Pre-Market Research Desk — Backend
===================================

This is a local Flask server that:
  1. Pulls pre-market data from Schwab API (price, options chains, historical bars)
  2. Pulls macro calendar from Finnhub API (economic events, earnings)
  3. Runs the supply/demand + volume analysis pipeline
  4. Serves the frontend dashboard at http://127.0.0.1:5000
  5. Silently tracks recommended setups and their outcomes in a local SQLite database

Run with:
    python app.py

First-run setup is documented in SETUP.md.
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

from schwab_client import SchwabClient
from finnhub_client import FinnhubClient
from analysis import analyze_instrument, rank_setups
from tracker import OutcomeTracker

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------
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

# ---------------------------------------------------------------
# Initialize clients and tracker
# ---------------------------------------------------------------
schwab = SchwabClient(
    app_key=config["schwab"]["app_key"],
    app_secret=config["schwab"]["app_secret"],
    callback_url=config["schwab"]["callback_url"],
    token_path=DATA_DIR / "schwab_tokens.json"
)

finnhub = FinnhubClient(api_key=config["finnhub"]["api_key"])
tracker = OutcomeTracker(db_path=DB_PATH)

# ---------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------
app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/scan", methods=["GET"])
def scan():
    """
    Main endpoint. Runs the full pre-market analysis pipeline and returns
    ranked trade setups for SPY and QQQ.
    """
    try:
        # 1. Pull macro calendar for today
        today = datetime.now(timezone.utc).date()

        if today.weekday() >= 5:
            return jsonify({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_closed": "Markets are closed on weekends.",
                "macro_summary": {"high_impact_events": [], "all_events": [], "earnings_today": []},
                "instruments": [],
                "tracker_stats": tracker.get_rolling_stats(days=30)
            })

        macro_events = finnhub.economic_calendar(today, today)
        earnings = finnhub.earnings_calendar(
            today, today,
            tickers=["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]
        )

        # 2. Analyze each instrument
        results = []
        for symbol in ["SPY", "QQQ"]:
            bars_daily = schwab.get_price_history(symbol, period_type="month", period=1,
                                                   frequency_type="daily", frequency=1)
            bars_hourly = schwab.get_price_history(symbol, period_type="day", period=5,
                                                    frequency_type="minute", frequency=30)
            quote = schwab.get_quote(symbol)

            analysis = analyze_instrument(
                symbol=symbol,
                daily_bars=bars_daily,
                intraday_bars=bars_hourly,
                quote=quote,
                macro_events=macro_events,
                earnings_events=earnings
            )
            results.append(analysis)

        # 3. Collect all candidates from all instruments, sort by conviction
        all_candidates = []
        for analysis in results:
            all_candidates.extend(analysis.get("all_candidates", []))
        all_candidates.sort(key=lambda s: s["conviction_score"], reverse=True)

        # Mix styles: prefer at least 1 scalp and 1 swing if both exist, up to 4 total
        scalps = [s for s in all_candidates if s.get("trade_style") == "scalp"]
        swings = [s for s in all_candidates if s.get("trade_style") == "swing"]
        seen_ids = set()
        top_setups = []
        if scalps:
            top_setups.append(scalps[0])
            seen_ids.add(id(scalps[0]))
        if swings:
            top_setups.append(swings[0])
            seen_ids.add(id(swings[0]))
        for s in all_candidates:
            if len(top_setups) >= 4:
                break
            if id(s) not in seen_ids:
                top_setups.append(s)
                seen_ids.add(id(s))

        # Fetch options chains only for setups we'll display
        from_str = today.isoformat()
        to_str = (today + timedelta(days=21)).isoformat()
        for setup in top_setups:
            chain = schwab.get_option_chain(
                symbol=setup["instrument"],
                contract_type=setup["direction"],
                from_date=from_str,
                to_date=to_str
            )
            setup["strike_tiers"] = pick_strike_tiers(chain, setup, today)

        # Build daily brief and pick best closest miss across instruments
        daily_brief = {}
        all_near_misses = []
        for analysis in results:
            sym = analysis["symbol"]
            daily_brief[sym] = {
                "trend": analysis.get("trend"),
                "zones_detected": analysis.get("zones_detected", 0),
                "demand_zones": analysis.get("demand_zones_count", 0),
                "supply_zones": analysis.get("supply_zones_count", 0),
                "volume_ratio": analysis.get("volume_ratio"),
            }
            if analysis.get("near_miss"):
                all_near_misses.append(analysis["near_miss"])
        closest_miss = (
            max(all_near_misses, key=lambda m: m["readiness_pct"])
            if all_near_misses else None
        )

        # Keep ranked instruments for backward compat
        ranked = rank_setups(results)

        # 4. Resolve outcomes of any open past recommendations before recording new ones
        tracker.check_outcomes(schwab)

        # 5. Silently log all displayed setups to the tracker
        for setup in top_setups:
            tracker.record_recommendation({"setup": setup})

        # 6. Get tracker stats for display
        stats = tracker.get_rolling_stats(days=30)

        return jsonify({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "macro_summary": summarize_macro(macro_events, earnings),
            "setups": top_setups,
            "instruments": ranked,
            "daily_brief": daily_brief,
            "closest_miss": closest_miss,
            "tracker_stats": stats
        })

    except Exception as e:
        app.logger.exception("scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/start", methods=["GET"])
def auth_start():
    """Returns the URL the user needs to visit to authorize Schwab."""
    return jsonify({"url": schwab.get_auth_url()})


@app.route("/api/auth/callback", methods=["POST"])
def auth_callback():
    """Exchanges the callback URL (with auth code) for tokens."""
    data = request.get_json()
    schwab.exchange_code(data["callback_url"])
    return jsonify({"status": "ok"})


@app.route("/api/history", methods=["GET"])
def history():
    """Return recent recommendations for the history view."""
    limit = min(int(request.args.get("limit", 30)), 100)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, recorded_at, symbol, direction, current_price,
               entry_trigger_price, stop, target, rr_ratio,
               conviction_score, conviction_label, outcome, outcome_at
        FROM recommendations
        ORDER BY recorded_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/health", methods=["GET"])
def health():
    finnhub_ok = False
    try:
        today = datetime.now(timezone.utc).date()
        finnhub.economic_calendar(today, today)
        finnhub_ok = True
    except Exception:
        pass
    return jsonify({
        "schwab_authed": schwab.is_authenticated(),
        "finnhub_key_valid": finnhub_ok,
        "tracker_db": str(DB_PATH)
    })


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def pick_strike_tiers(chain, setup, today):
    if not chain or not chain.get("contracts"):
        return []

    is_scalp = setup.get("trade_style") == "scalp"
    conviction = setup.get("conviction_label", "Medium")
    macro_hostile = setup.get("macro", {}).get("hostile", False)

    tier_specs = [
        {"tier": "aggressive", "dte_min": 0, "dte_max": 1 if is_scalp else 3,  "target_delta": 0.60},
        {"tier": "moderate",   "dte_min": 2 if is_scalp else 7,  "dte_max": 5 if is_scalp else 14, "target_delta": 0.45},
        {"tier": "conservative","dte_min": 6 if is_scalp else 14, "dte_max": 14 if is_scalp else 21, "target_delta": 0.35},
    ]

    if macro_hostile or conviction == "Low":
        rec_tier = "conservative"
    elif is_scalp and conviction == "High":
        rec_tier = "aggressive"
    else:
        rec_tier = "moderate"

    tiers = []
    for spec in tier_specs:
        best = None
        best_dist = float("inf")
        for c in chain["contracts"]:
            if c.get("delta") is None:
                continue
            try:
                exp_date = datetime.strptime(c["expiration"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                continue
            dte = (exp_date - today).days
            if dte < spec["dte_min"] or dte > spec["dte_max"]:
                continue
            dist = abs(abs(c["delta"]) - spec["target_delta"])
            if dist < best_dist:
                best_dist = dist
                best = c
                best_dte = dte
        if best:
            bid = best.get("bid")
            ask = best.get("ask")
            mid = round((bid + ask) / 2, 2) if bid is not None and ask is not None else None
            tiers.append({
                "tier": spec["tier"],
                "recommended": spec["tier"] == rec_tier,
                "symbol": best.get("symbol", f"{setup['instrument']} {best['strike']}{setup['direction'][0]}"),
                "strike": best.get("strike"),
                "expiration": best.get("expiration"),
                "dte": best_dte,
                "delta": best.get("delta"),
                "theta": best.get("theta"),
                "iv": best.get("iv"),
                "bid": bid,
                "ask": ask,
                "mid": mid,
            })

    # If rec_tier had no contracts, star the middle available tier
    if tiers and not any(t["recommended"] for t in tiers):
        tiers[len(tiers) // 2]["recommended"] = True

    return tiers


def summarize_macro(events, earnings):
    high_impact = [e for e in events if e.get("impact") == "high"]
    return {
        "high_impact_events": high_impact,
        "all_events": events,
        "earnings_today": earnings
    }


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("Pre-Market Research Desk")
    print("=" * 60)
    print(f"Schwab authenticated: {schwab.is_authenticated()}")
    print(f"Frontend directory:   {FRONTEND_DIR}")
    print(f"Tracker database:     {DB_PATH}")
    print("=" * 60)
    print("Open http://127.0.0.1:5000 in your browser.")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False)
