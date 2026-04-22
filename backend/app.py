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

            # Only fetch options chain if analysis found a viable setup
            if analysis["setup"]:
                chain = schwab.get_option_chain(
                    symbol=symbol,
                    contract_type=analysis["setup"]["direction"],
                    target_date=analysis["setup"]["target_expiry"]
                )
                analysis["setup"]["strike_recommendation"] = pick_strike(
                    chain, analysis["setup"]
                )

            results.append(analysis)

        # 3. Rank setups and pick top 1-2
        ranked = rank_setups(results)

        # 4. Silently log recommendations to the tracker
        for r in ranked:
            if r.get("setup"):
                tracker.record_recommendation(r)

        # 5. Get tracker stats for display
        stats = tracker.get_rolling_stats(days=30)

        return jsonify({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "macro_summary": summarize_macro(macro_events, earnings),
            "instruments": ranked,
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


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "schwab_authed": schwab.is_authenticated(),
        "finnhub_key_set": bool(config["finnhub"]["api_key"]),
        "tracker_db": str(DB_PATH)
    })


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def pick_strike(chain, setup):
    """Pick a strike based on setup direction and expected move."""
    if not chain or "contracts" not in chain:
        return None
    current = setup["current_price"]
    direction = setup["direction"]
    # Target ~0.40 delta for directional swing trades; user can adjust
    target_delta = 0.40 if direction == "CALL" else -0.40
    best = None
    best_dist = float("inf")
    for c in chain["contracts"]:
        if c.get("delta") is None:
            continue
        dist = abs(c["delta"] - target_delta)
        if dist < best_dist:
            best_dist = dist
            best = c
    return best


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
