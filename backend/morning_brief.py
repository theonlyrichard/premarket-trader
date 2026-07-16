"""
Premarket Discord morning brief producer.

Runs the same SPY/QQQ pipeline as the Flask desk and prints a Discord-ready
markdown brief to stdout (for Hermes cron no_agent=True).

Usage:
    python morning_brief.py
    python morning_brief.py --json   # also dump JSON to stderr path optional

Exit codes:
    0 success (even if NO TRADE / map-only)
    2 auth/config failure
    3 unexpected pipeline failure
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from schwab_client import SchwabClient
from finnhub_client import FinnhubClient
from analysis import (
    analyze_instrument,
    rank_setups,
    build_morning_plan,
    current_desk_phase,
    now_et,
    now_pt,
)
from tracker import OutcomeTracker


CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"
TOKEN_PATH = DATA_DIR / "schwab_tokens.json"


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("config.json missing")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def fmt_px(x):
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return "—"


def run_pipeline():
    cfg = load_config()
    DATA_DIR.mkdir(exist_ok=True)
    schwab = SchwabClient(
        app_key=cfg["schwab"]["app_key"],
        app_secret=cfg["schwab"]["app_secret"],
        callback_url=cfg["schwab"]["callback_url"],
        token_path=TOKEN_PATH,
    )
    finnhub = FinnhubClient(api_key=cfg.get("finnhub", {}).get("api_key", ""))
    tracker = OutcomeTracker(db_path=DB_PATH)

    today = now_et().date()
    phase = current_desk_phase()

    if today.weekday() >= 5:
        return {
            "weekend": True,
            "phase": "closed",
            "morning_plan": None,
            "recommendations": [],
            "instruments": [],
            "macro_summary": {},
        }

    try:
        macro_events = finnhub.economic_calendar(today, today) or []
    except Exception:
        macro_events = []
    try:
        earnings = finnhub.earnings_calendar(
            today, today,
            tickers=["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"],
        ) or []
    except Exception:
        earnings = []

    results = []
    for symbol in ["SPY", "QQQ"]:
        bars_daily = schwab.get_price_history(
            symbol, period_type="month", period=6,
            frequency_type="daily", frequency=1,
        )
        bars_5m = schwab.get_price_history(
            symbol, period_type="day", period=5,
            frequency_type="minute", frequency=5,
        )
        bars_15m = schwab.get_price_history(
            symbol, period_type="day", period=10,
            frequency_type="minute", frequency=15,
        )
        bars_30m = schwab.get_price_history(
            symbol, period_type="day", period=10,
            frequency_type="minute", frequency=30,
        )
        if not bars_15m or len(bars_15m) < 20:
            bars_15m = bars_30m
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
            bars_1h=bars_30m,
        )
        # Premarket: require option liquidity for live print
        if analysis.get("setup") and phase == "premarket":
            # Chain pick is optional for brief; leave setup if present.
            # Full liquidity gate is done in Flask app. For brief we just report.
            pass
        results.append(analysis)

    ranked = rank_setups(results, max_recs=2)
    recommendations = [
        r for r in ranked if r.get("setup") and r.get("recommended")
    ]
    try:
        tracker.check_outcomes(schwab)
        for r in recommendations:
            tracker.record_recommendation(r)
    except Exception:
        pass

    macro_summary = {
        "high_impact_events": [e for e in macro_events if e.get("impact") == "high"],
        "earnings_today": earnings,
        "all_events": macro_events,
        "note": "Hostile macro is a FLAG only — setups are not auto-killed.",
    }
    morning_plan = build_morning_plan(
        ranked, recommendations, phase, macro_summary=macro_summary
    )
    return {
        "weekend": False,
        "phase": phase,
        "morning_plan": morning_plan,
        "recommendations": recommendations,
        "instruments": ranked,
        "macro_summary": macro_summary,
        "clock": {
            "et": now_et().strftime("%Y-%m-%d %H:%M %Z"),
            "pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
        },
    }


def format_discord_brief(payload: dict) -> str:
    """Compact Discord markdown for #overview."""
    if payload.get("weekend"):
        return (
            "**Premarket desk · weekend**\n"
            "Markets closed. No brief.\n"
            "_Weeklies desk · flat ~8:30 AM PT on trading days_"
        )

    plan = payload.get("morning_plan") or {}
    phase = payload.get("phase") or plan.get("session_phase") or "?"
    clock = payload.get("clock") or plan.get("clock") or {}
    pt = clock.get("pt") or now_pt().strftime("%Y-%m-%d %H:%M %Z")

    lines = []
    lines.append(f"**Premarket desk · {pt}**")
    lines.append(f"**{plan.get('headline') or 'SCAN'}** · grade `{plan.get('grade', '—')}` · phase `{phase}`")
    lines.append("")
    lines.append("**Bias**")
    lines.append(plan.get("market_bias") or "—")
    if plan.get("secondary_bias"):
        lines.append(plan["secondary_bias"])

    # Actions
    actions = plan.get("actions") or []
    if actions:
        lines.append("")
        lines.append("**Plan**")
        for a in actions[:5]:
            lines.append(f"• {a}")

    # Live entries
    recs = payload.get("recommendations") or []
    lines.append("")
    if recs:
        lines.append(f"**Live option entries ({len(recs)})**")
        for r in recs:
            s = r.get("setup") or {}
            oe = s.get("option_entry") or s.get("strike_recommendation") or {}
            strike = oe.get("strike") or oe.get("symbol") or "?"
            exp = oe.get("expiration") or s.get("target_expiry") or "?"
            style = oe.get("style") or s.get("expiry_style") or "weekly"
            lines.append(
                f"• **{s.get('instrument')} {s.get('direction')}** "
                f"{s.get('pattern')} ({s.get('zone_timeframe') or '?'}) · "
                f"score {s.get('conviction_score')}/10 · R:R 1:{s.get('rr_ratio')}"
            )
            lines.append(
                f"  entry: {s.get('entry_trigger')}\n"
                f"  stop {fmt_px(s.get('stop'))} · target {fmt_px(s.get('target'))}\n"
                f"  option: {strike} exp {exp} ({style}) mid {oe.get('debit_mid') or oe.get('mid') or '—'}"
            )
    else:
        lines.append("**Live option entries: NONE** _(no trade is success-mode)_")

    # Per instrument map
    for inst_plan in plan.get("instruments") or []:
        sym = inst_plan.get("symbol")
        lines.append("")
        lines.append(f"**{sym} map** · trend `{inst_plan.get('trend')}`")
        # top levels
        lvls = inst_plan.get("key_levels") or []
        if lvls:
            bits = [f"{L['name']} **{fmt_px(L['price'])}**" for L in lvls[:5]]
            lines.append("Levels: " + " · ".join(bits))
        # watches
        for w in (inst_plan.get("watch_list") or [])[:3]:
            if w.get("status") == "rejected":
                lines.append(f"• blocked: {w.get('condition')}")
            else:
                lines.append(
                    f"• watch {w.get('side')} {w.get('pattern')} @ {fmt_px(w.get('proximal'))} "
                    f"({w.get('status')})"
                )
        why = inst_plan.get("why_no_trade") or []
        if why and not inst_plan.get("has_live_entry"):
            lines.append("Why no print: " + "; ".join(
                w.replace("**", "") for w in why[:2]
            ))

    # Zone map top
    instruments = payload.get("instruments") or []
    zone_lines = []
    for inst in instruments:
        for z in (inst.get("zone_map") or [])[:2]:
            zone_lines.append(
                f"• {inst.get('symbol')} `{z.get('timeframe')}` "
                f"{z.get('type')} {z.get('pattern')} "
                f"{fmt_px(z.get('bottom'))}–{fmt_px(z.get('top'))} "
                f"{'FRESH' if z.get('fresh') else 'partial' if z.get('partial') else ''}"
            )
    if zone_lines:
        lines.append("")
        lines.append("**Top TOS boxes**")
        lines.extend(zone_lines[:6])

    # Orderflow proxy (not true OF)
    of_lines = []
    for inst in instruments:
        ofp = inst.get("orderflow_proxy") or {}
        if not ofp.get("available"):
            continue
        vp = ofp.get("volume_profile") or {}
        rv = ofp.get("relative_volume") or {}
        of_lines.append(
            f"• **{inst.get('symbol')}** RVOL {rv.get('rvol')} ({rv.get('label')}) · "
            f"{(ofp.get('imbalance_proxy') or {}).get('bias')} · "
            f"POC {fmt_px(vp.get('poc'))} VA {fmt_px(vp.get('val'))}–{fmt_px(vp.get('vah'))} · "
            f"`{ofp.get('confirmation')}`"
        )
    if of_lines:
        lines.append("")
        lines.append("**Orderflow proxy** _(OHLC+vol only — not footprint)_")
        lines.extend(of_lines[:4])

    # Macro
    ms = payload.get("macro_summary") or {}
    flags = []
    for e in (ms.get("high_impact_events") or [])[:3]:
        flags.append(e.get("event") or "event")
    for e in (ms.get("earnings_today") or [])[:3]:
        flags.append(f"{e.get('symbol')} ER")
    if flags:
        lines.append("")
        lines.append("**Macro flags (not veto):** " + ", ".join(flags))

    lines.append("")
    lines.append(
        "_Carmine desk · weeklies default · 0DTE A-tier only · "
        "flat ~8:30 AM PT · open http://127.0.0.1:5000 for full plan_"
    )
    lines.append("_Not financial advice · local desk brief_")
    return "\n".join(lines)


def main():
    try:
        payload = run_pipeline()
    except Exception as e:
        err = str(e)
        if "400" in err or "401" in err or "token" in err.lower() or "oauth" in err.lower():
            msg = (
                "**Premarket desk · AUTH FAIL**\n"
                "Schwab refresh expired. Run on the desk machine:\n"
                "`cd backend && python setup_auth.py` then restart flask.\n"
                f"Detail: `{err[:180]}`"
            )
            print(msg)
            return 2
        print(
            "**Premarket desk · BRIEF ERROR**\n"
            f"`{err[:240]}`\n"
            "Check Flask tokens / network."
        )
        traceback.print_exc(file=sys.stderr)
        return 3

    text = format_discord_brief(payload)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
