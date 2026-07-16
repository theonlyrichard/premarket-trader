"""
Live zone alerts for Premarket Trader.

Polls SPY/QQQ (premarket + RTH open→11:30 ET only) against multi-TF S/D zones.
On new events (APPROACH / TAG / INVALIDATE), prints Discord markdown.
Empty stdout = silent (cron watchdog pattern).

State: data/zone_alert_state.json (dedupe + cooldowns)

Usage:
    python zone_alerts.py            # normal tick
    python zone_alerts.py --force    # ignore cooldowns (testing)
    python zone_alerts.py --status   # print JSON status, no alerts
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from schwab_client import SchwabClient
from analysis import (
    analyze_instrument,
    build_zone_map,
    current_desk_phase,
    detect_zones,
    detect_htf_zones,
    aggregate_bars,
    now_et,
    now_pt,
    serialize_zone,
)

DATA_DIR = BASE_DIR / "data"
STATE_PATH = DATA_DIR / "zone_alert_state.json"
CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = DATA_DIR / "schwab_tokens.json"

# Fraction of zone height as approach band; floor in points for SPY/QQQ
APPROACH_FRACTION = 0.35
APPROACH_MIN_PTS = 0.35
APPROACH_MAX_PTS = 1.75

# Cooldown between re-alerts of same zone+kind (seconds)
COOLDOWN_APPROACH = 20 * 60
COOLDOWN_TAG = 12 * 60
COOLDOWN_INVALIDATE = 30 * 60

ALERTABLE_PHASES = {"premarket", "rth_session"}


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if not STATE_PATH.exists():
        return {"events": {}, "last_tick": None, "prices": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"events": {}, "last_tick": None, "prices": {}}


def save_state(state):
    DATA_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def approach_band(zone, price):
    height = abs((zone.get("top") or 0) - (zone.get("bottom") or 0))
    band = max(APPROACH_MIN_PTS, min(APPROACH_MAX_PTS, height * APPROACH_FRACTION))
    if price and price > 100:
        # slight scale for higher absolute names
        band = max(band, price * 0.0004)
    return band


def zone_key(symbol, z):
    return (
        f"{symbol}|{z.get('timeframe')}|{z.get('type')}|"
        f"{round(float(z.get('top') or 0), 2)}|{round(float(z.get('bottom') or 0), 2)}"
    )


def classify(price, zone):
    """
    Return event kind or None:
      tag — price trades through zone range
      approach — within band outside the zone
      invalidate — previously tagged and closed through distal (simplified)
    """
    top = float(zone["top"])
    bottom = float(zone["bottom"])
    band = approach_band(zone, price)
    ztype = zone.get("type")

    if bottom <= price <= top:
        return "tag"

    # approach from outside
    if price > top and (price - top) <= band:
        return "approach"
    if price < bottom and (bottom - price) <= band:
        return "approach"

    return None


def invalidation_check(price, zone, was_tagged):
    if not was_tagged:
        return False
    distal = float(zone.get("distal") or (
        zone["bottom"] if zone.get("type") == "demand" else zone["top"]
    ))
    if zone.get("type") == "demand":
        return price < distal
    return price > distal


def cooldown_ok(state, key, kind, force=False):
    if force:
        return True
    events = state.setdefault("events", {})
    rec = events.get(key) or {}
    last = rec.get(f"last_{kind}")
    if not last:
        return True
    cd = {
        "approach": COOLDOWN_APPROACH,
        "tag": COOLDOWN_TAG,
        "invalidate": COOLDOWN_INVALIDATE,
    }.get(kind, 900)
    try:
        age = time.time() - float(last)
    except Exception:
        return True
    return age >= cd


def mark_event(state, key, kind, extra=None):
    events = state.setdefault("events", {})
    rec = events.get(key) or {}
    rec[f"last_{kind}"] = time.time()
    rec["last_kind"] = kind
    if kind == "tag":
        rec["tagged"] = True
    if kind == "invalidate":
        rec["invalidated"] = True
        rec["tagged"] = False
    if extra:
        rec.update(extra)
    events[key] = rec


def fetch_symbol_zones(schwab, symbol):
    bars_daily = schwab.get_price_history(
        symbol, period_type="month", period=6,
        frequency_type="daily", frequency=1,
    )
    bars_5m = schwab.get_price_history(
        symbol, period_type="day", period=3,
        frequency_type="minute", frequency=5,
    )
    bars_15m = schwab.get_price_history(
        symbol, period_type="day", period=5,
        frequency_type="minute", frequency=15,
    )
    bars_30m = schwab.get_price_history(
        symbol, period_type="day", period=5,
        frequency_type="minute", frequency=30,
    )
    if not bars_15m or len(bars_15m) < 20:
        bars_15m = bars_30m
    quote = schwab.get_quote(symbol)
    q = quote.get("quote") or {}
    price = q.get("lastPrice") or q.get("mark") or q.get("extendedHoursPrice")
    if not price and bars_5m:
        price = bars_5m[-1]["close"]
    if not price and bars_15m:
        price = bars_15m[-1]["close"]

    zones_5m = detect_zones(bars_5m, lookback_bars=120, timeframe="5m") if len(bars_5m) >= 20 else []
    zones_15m = detect_zones(bars_15m, lookback_bars=100, timeframe="15m") if len(bars_15m) >= 20 else []
    bars_1h = aggregate_bars(bars_15m, 4) if bars_15m else []
    zones_1h = detect_zones(bars_1h, lookback_bars=60, timeframe="1H") if len(bars_1h) >= 20 else []
    zones_d = detect_htf_zones(bars_daily)

    zones_by_tf = {"5m": zones_5m, "15m": zones_15m, "1H": zones_1h, "D": zones_d}
    # Prefer alertable: unmitigated, high quality, near money — from build_zone_map
    zone_map = build_zone_map(zones_by_tf, float(price or 0), limit=10)
    # Also keep raw non-serial fields for distal if missing — map already serial
    return float(price or 0), zone_map, quote


def format_alert(symbol, kind, z, price, phase):
    emoji = {"approach": "👀", "tag": "🎯", "invalidate": "❌"}.get(kind, "•")
    title = {"approach": "APPROACH", "tag": "TAG", "invalidate": "INVALIDATE"}.get(kind, kind.upper())
    box = f"{z.get('bottom')}–{z.get('top')}"
    prox = z.get("proximal")
    dist = z.get("distal")
    line = (
        f"{emoji} **{symbol} {title}** · `{z.get('timeframe')}` "
        f"**{z.get('type')}** {z.get('pattern')} · {box}\n"
        f"Last **{price:.2f}** · proximal {prox} / distal {dist} · "
        f"{'FRESH' if z.get('fresh') else 'partial' if z.get('partial') else '—'} · phase `{phase}`\n"
    )
    if kind == "approach":
        line += f"_Price nearing zone — sit for agency. TOS: {z.get('tos_draw') or box}_\n"
    elif kind == "tag":
        line += (
            f"_In the box — wait reversal/agency before weekly { 'CALL' if z.get('type')=='demand' else 'PUT' }. "
            f"Not auto-entry._\n"
        )
    else:
        line += "_Closed through distal — zone thesis dead for now. Stand down._\n"
    line += f"_pt {now_pt().strftime('%H:%M %Z')} · not financial advice_"
    return line


def tick(force=False, status_only=False):
    phase = current_desk_phase()
    state = load_state()
    state["last_tick"] = now_pt().isoformat()
    state["phase"] = phase

    if phase not in ALERTABLE_PHASES and not force:
        # Silent outside windows
        if status_only:
            print(json.dumps({"phase": phase, "alerts": 0, "silent": True}))
        else:
            # empty stdout for watchdog
            pass
        save_state(state)
        return 0

    if now_et().weekday() >= 5 and not force:
        save_state(state)
        return 0

    cfg = load_config()
    schwab = SchwabClient(
        app_key=cfg["schwab"]["app_key"],
        app_secret=cfg["schwab"]["app_secret"],
        callback_url=cfg["schwab"]["callback_url"],
        token_path=TOKEN_PATH,
    )

    alerts = []
    status_bodies = []

    for symbol in ["SPY", "QQQ"]:
        try:
            price, zone_map, _quote = fetch_symbol_zones(schwab, symbol)
        except Exception as e:
            err = str(e)
            if any(x in err for x in ("401", "400", "oauth", "token", "Token")):
                print(
                    "**Zone alerts · AUTH FAIL**\n"
                    "Schwab refresh expired — run `python setup_auth.py` in backend.\n"
                    f"`{err[:160]}`"
                )
                return 2
            # soft fail other symbols
            continue

        state.setdefault("prices", {})[symbol] = price
        status_bodies.append({"symbol": symbol, "price": price, "zones": len(zone_map)})

        for z in zone_map:
            key = zone_key(symbol, z)
            prev = (state.get("events") or {}).get(key) or {}
            was_tagged = bool(prev.get("tagged")) and not prev.get("invalidated")

            if invalidation_check(price, z, was_tagged or prev.get("last_kind") == "tag"):
                if cooldown_ok(state, key, "invalidate", force=force):
                    mark_event(state, key, "invalidate", {"price": price})
                    alerts.append(format_alert(symbol, "invalidate", z, price, phase))
                continue

            kind = classify(price, z)
            if not kind:
                continue
            if kind == "approach" and was_tagged:
                # already in/through — don't spam approach
                continue
            if cooldown_ok(state, key, kind, force=force):
                mark_event(state, key, kind, {"price": price})
                alerts.append(format_alert(symbol, kind, z, price, phase))

    save_state(state)

    if status_only:
        print(json.dumps({
            "phase": phase,
            "prices": state.get("prices"),
            "alerts_this_tick": len(alerts),
            "symbols": status_bodies,
        }, indent=2))
        return 0

    if not alerts:
        return 0  # silent

    # Cap spam
    header = f"**Zone radar · {now_pt().strftime('%Y-%m-%d %H:%M %Z')} · `{phase}`**"
    body = "\n\n".join(alerts[:6])
    print(f"{header}\n\n{body}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    try:
        return tick(force=args.force, status_only=args.status)
    except SystemExit as e:
        raise
    except Exception as e:
        print(f"**Zone alerts · ERROR**\n`{e}`")
        return 3


if __name__ == "__main__":
    sys.exit(main() or 0)
