"""
Analysis engine — Carmine / InvestiTrade–aligned desk.

Pipeline:
  1. Session tagging (overnight / premarket / RTH) from extended-hour bars
  2. Premarket levels pack (ONH/ONL, gap, prior day, premarket range)
  3. Multi-TF supply & demand (HTF context + LTF RBD/DBR zones)
  4. Volume confirmation + tech confluence
  5. Strict setup construction (proximal/distal, weekly options default)
  6. Factor score (NOT probability). Hostile macro = flag only.

Timezone note: market session boundaries use US/Eastern equivalents via
fixed UTC offsets for EST/EDT approximation is avoided — we use local
struct using fixed America/New_York windows via timestamp math that assumes
the candle datetimes from Schwab are epoch ms in the bar clock. Bars are
tagged with hour:minute in America/New_York when possible.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# Market clocks (America/New_York)
PREMARKET_START = (4, 0)   # 4:00 ET
RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)
SESSION_FLAT_ET = (11, 30)  # Carmine-style cut ≈ 8:30 AM PT


# ===================================================================
# TIME / SESSION HELPERS
# ===================================================================

def _bar_dt(bar: dict) -> Optional[datetime]:
    ms = bar.get("datetime")
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET)
    except Exception:
        return None


def tag_bar_session(bar: dict) -> str:
    """overnight | premarket | rth | afterhours"""
    dt = _bar_dt(bar)
    if not dt:
        return "unknown"
    t = (dt.hour, dt.minute)
    if t >= RTH_CLOSE or t < PREMARKET_START:
        # 16:00–23:59 and 00:00–03:59
        if t >= RTH_CLOSE:
            return "afterhours"
        return "overnight"
    if t < RTH_OPEN:
        return "premarket"
    return "rth"


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def now_pt() -> datetime:
    return datetime.now(timezone.utc).astimezone(PT)


def current_desk_phase() -> str:
    """
    premarket | rth_session | post_session | closed
    RTH "session" for this desk runs open → Carmine cut (11:30 ET).
    """
    n = now_et()
    if n.weekday() >= 5:
        return "closed"
    t = (n.hour, n.minute)
    if t < PREMARKET_START:
        return "overnight"
    if t < RTH_OPEN:
        return "premarket"
    if t < SESSION_FLAT_ET:
        return "rth_session"
    if t < RTH_CLOSE:
        return "post_session"
    return "afterhours"


def next_friday(from_date=None):
    today = from_date or now_et().date()
    days = (4 - today.weekday()) % 7
    if days == 0:
        # If already Friday during session, still that Friday for weeklies;
        # for 0DTE we use today separately.
        return today
    return today + timedelta(days=days)


def envelope_for_0dte_eligible(score: int, zone_fresh: bool, playbook_pass: bool) -> bool:
    """0DTE only when A-tier quality."""
    return playbook_pass and zone_fresh and score >= 8


# ===================================================================
# PREMARKET / LEVELS PACK
# ===================================================================

def prior_day_levels(daily_bars: list) -> Optional[dict]:
    if len(daily_bars) < 2:
        return None
    prev = daily_bars[-2] if _is_today_bar(daily_bars[-1]) else daily_bars[-1]
    # If last daily bar is still forming today, prior day is -2; else last complete is -1
    # Prefer -2 when last bar date == today ET
    last = daily_bars[-1]
    last_dt = _bar_dt(last)
    today = now_et().date()
    if last_dt and last_dt.date() == today and len(daily_bars) >= 2:
        prev = daily_bars[-2]
    else:
        prev = daily_bars[-1]
    return {
        "high": prev["high"],
        "low": prev["low"],
        "close": prev["close"],
        "open": prev.get("open", prev["close"]),
    }


def _is_today_bar(bar: dict) -> bool:
    dt = _bar_dt(bar)
    return bool(dt and dt.date() == now_et().date())


def build_premarket_pack(intraday_bars: list, daily_bars: list, current_price: float) -> dict:
    """
    First-class premarket levels pack:
      overnight high/low/mid, premarket H/L/range, gap, prior day H/L/C, bias notes
    """
    prior = prior_day_levels(daily_bars) or {}
    today = now_et().date()

    overnight_bars = []
    premkt_bars = []
    rth_today = []

    for b in intraday_bars:
        dt = _bar_dt(b)
        if not dt:
            continue
        sess = tag_bar_session(b)
        # Overnight for "today's open context" = after prior RTH close through 4:00 ET today
        # Approx: afterhours yesterday + overnight early today + early minutes
        if dt.date() == today:
            if sess == "premarket":
                premkt_bars.append(b)
            elif sess == "rth":
                rth_today.append(b)
            elif sess in ("overnight", "afterhours"):
                overnight_bars.append(b)
        elif dt.date() == today - timedelta(days=1) and sess == "afterhours":
            overnight_bars.append(b)

    def _hl(bars):
        if not bars:
            return None, None
        return max(b["high"] for b in bars), min(b["low"] for b in bars)

    onh, onl = _hl(overnight_bars)
    pmh, pml = _hl(premkt_bars)

    # Combined "globex-ish" range: overnight + premarket
    glo_bars = overnight_bars + premkt_bars
    gh, gl = _hl(glo_bars)

    prior_close = prior.get("close")
    gap = None
    gap_pct = None
    gap_dir = "flat"
    if prior_close and current_price:
        gap = current_price - prior_close
        gap_pct = (gap / prior_close) * 100
        if gap > 0.15:
            gap_dir = "up"
        elif gap < -0.15:
            gap_dir = "down"

    # Bias from price vs overnight mid / prior close
    bias_notes = []
    if gh is not None and gl is not None:
        mid = (gh + gl) / 2
        if current_price > mid:
            bias_notes.append("price_above_overnight_mid")
        elif current_price < mid:
            bias_notes.append("price_below_overnight_mid")
    if prior_close:
        if current_price > prior_close:
            bias_notes.append("trading_above_prior_close")
        else:
            bias_notes.append("trading_below_prior_close")
    if pmh is not None and pml is not None:
        if current_price >= pmh * 0.999:
            bias_notes.append("at_or_near_premarket_high")
        if current_price <= pml * 1.001:
            bias_notes.append("at_or_near_premarket_low")

    pack = {
        "prior_day": prior or None,
        "overnight_high": onh,
        "overnight_low": onl,
        "overnight_mid": ((onh + onl) / 2) if onh is not None and onl is not None else None,
        "premarket_high": pmh,
        "premarket_low": pml,
        "premarket_range": (pmh - pml) if pmh is not None and pml is not None else None,
        "globex_high": gh,
        "globex_low": gl,
        "gap_points": round(gap, 2) if gap is not None else None,
        "gap_pct": round(gap_pct, 3) if gap_pct is not None else None,
        "gap_direction": gap_dir,
        "bias_notes": bias_notes,
        "premarket_bar_count": len(premkt_bars),
        "overnight_bar_count": len(overnight_bars),
        "session_phase": current_desk_phase(),
        "clock": {
            "et": now_et().strftime("%Y-%m-%d %H:%M %Z"),
            "pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
        },
    }
    return pack


# ===================================================================
# ZONE DETECTION (playbook labels)
# ===================================================================

def _origin_time_et(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET).strftime(
            "%Y-%m-%d %H:%M ET"
        )
    except Exception:
        return None


def detect_zones(intraday_bars, lookback_bars=120, timeframe="15m"):
    """
    Detect supply/demand bases with Carmine-style labels:
      RBD  — rally base drop  → supply
      DBR  — drop base rally  → demand
      compression_break_up / compression_break_down

    Designed to mark the *same price geometry* you draw on TOS from these bars
    (same Schwab candle family as the API — not pixel screen-grab of TOS).
    """
    if len(intraday_bars) < 20:
        return []

    bars = intraday_bars[-lookback_bars:]
    ranges = [b["high"] - b["low"] for b in bars]
    avg_range = statistics.mean(ranges) if ranges else 0
    if avg_range <= 0:
        return []

    # TF-aware sensitivity: finer charts need slightly stricter agency
    exp_mult = {"5m": 1.65, "15m": 1.5, "1H": 1.4, "D": 1.35}.get(timeframe, 1.5)
    base_tight = {"5m": 0.85, "15m": 0.9, "1H": 0.95, "D": 1.0}.get(timeframe, 0.9)
    body_ratio = 0.55 if timeframe in ("1H", "D") else 0.6

    zones = []
    i = 2
    while i < len(bars) - 1:
        curr = bars[i]
        curr_range = curr["high"] - curr["low"]
        curr_body = abs(curr["close"] - curr["open"])
        is_explosive = curr_range > avg_range * exp_mult and curr_body > curr_range * body_ratio

        if is_explosive:
            # Prefer 2–4 bar base (classic RBD/DBR footprint)
            best = None
            for width in (2, 3, 4, 1):
                base_start = max(0, i - width)
                base_bars = bars[base_start:i]
                if not base_bars:
                    continue
                base_ranges = [b["high"] - b["low"] for b in base_bars]
                avg_base_range = statistics.mean(base_ranges)
                if avg_base_range < avg_range * base_tight:
                    score = (avg_range / max(avg_base_range, 1e-9)) * width
                    if best is None or score > best[0]:
                        best = (score, base_bars, avg_base_range)

            if best:
                _, base_bars, avg_base_range = best
                zone_top = max(b["high"] for b in base_bars)
                zone_bottom = min(b["low"] for b in base_bars)
                # ignore micro-noise bases
                if (zone_top - zone_bottom) < avg_range * 0.15:
                    i += 1
                    continue

                bullish_leave = curr["close"] > curr["open"]
                direction = "demand" if bullish_leave else "supply"

                pre = bars[max(0, i - len(base_bars) - 4): i - len(base_bars)]
                pre_dir = (pre[-1]["close"] - pre[0]["open"]) if len(pre) >= 2 else 0
                compressed = avg_base_range < avg_range * 0.55
                if compressed:
                    pattern = "compression_break_up" if bullish_leave else "compression_break_down"
                elif bullish_leave:
                    pattern = "DBR" if pre_dir <= 0 else "continuation_demand"
                else:
                    pattern = "RBD" if pre_dir >= 0 else "continuation_supply"

                revisit_count = 0
                for j in range(i + 1, len(bars)):
                    if bars[j]["low"] <= zone_top and bars[j]["high"] >= zone_bottom:
                        revisit_count += 1
                fresh = revisit_count == 0
                mitigated = False
                partial = False
                if revisit_count:
                    partial = True
                    for j in range(i + 1, len(bars)):
                        if direction == "demand" and bars[j]["close"] < zone_bottom:
                            mitigated = True
                            break
                        if direction == "supply" and bars[j]["close"] > zone_top:
                            mitigated = True
                            break
                    if mitigated:
                        partial = False

                agency = curr_range / avg_range if avg_range else 1.0
                leave_pts = abs(curr["close"] - (zone_top if bullish_leave else zone_bottom))

                zones.append({
                    "type": direction,
                    "pattern": pattern,
                    "timeframe": timeframe,
                    "top": round(zone_top, 2),
                    "bottom": round(zone_bottom, 2),
                    "proximal": round(zone_top if direction == "demand" else zone_bottom, 2),
                    "distal": round(zone_bottom if direction == "demand" else zone_top, 2),
                    "origin_index": i,
                    "origin_time": curr.get("datetime"),
                    "origin_time_et": _origin_time_et(curr.get("datetime")),
                    "origin_session": tag_bar_session(curr),
                    "strength": agency,
                    "agency": round(agency, 2),
                    "leave_points": round(leave_pts, 2),
                    "fresh": fresh and not mitigated,
                    "partial": partial and not mitigated,
                    "mitigated": mitigated,
                    "revisit_count": revisit_count,
                    "tos_draw": (
                        f"TOS {timeframe}: draw box {zone_bottom:.2f}–{zone_top:.2f} "
                        f"({pattern} {direction}, origin {_origin_time_et(curr.get('datetime')) or 'n/a'})"
                    ),
                })
                i += 1
        i += 1

    return zones


def detect_htf_zones(daily_bars, lookback=50):
    if len(daily_bars) < 15:
        return []
    return detect_zones(daily_bars, lookback_bars=lookback, timeframe="D")


def aggregate_bars(bars, group_size):
    """Aggregate smaller TF candles into higher TF (e.g. 15m×4 → ~1H)."""
    if not bars or group_size <= 1:
        return list(bars or [])
    out = []
    for i in range(0, len(bars), group_size):
        chunk = bars[i:i + group_size]
        if not chunk:
            continue
        out.append({
            "datetime": chunk[0].get("datetime"),
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "volume": sum((b.get("volume") or 0) for b in chunk),
        })
    return out


def zone_quality_score(z):
    score = 0.0
    if z.get("fresh"):
        score += 3
    elif z.get("partial"):
        score += 1.5
    if z.get("mitigated"):
        score -= 5
    score += min(z.get("agency") or 0, 4)
    # prefer classic labels
    if z.get("pattern") in ("DBR", "RBD", "compression_break_up", "compression_break_down"):
        score += 1
    # timeframe weight for map display
    score += {"D": 2.5, "1H": 2.0, "15m": 1.5, "5m": 1.0}.get(z.get("timeframe"), 1.0)
    return score


def serialize_zone(z, current_price=None):
    if not z:
        return None
    out = {
        "type": z.get("type"),
        "pattern": z.get("pattern"),
        "timeframe": z.get("timeframe"),
        "top": z.get("top"),
        "bottom": z.get("bottom"),
        "proximal": z.get("proximal"),
        "distal": z.get("distal"),
        "agency": z.get("agency"),
        "fresh": z.get("fresh"),
        "partial": z.get("partial"),
        "mitigated": z.get("mitigated"),
        "revisit_count": z.get("revisit_count"),
        "origin_time_et": z.get("origin_time_et"),
        "origin_session": z.get("origin_session"),
        "tos_draw": z.get("tos_draw"),
        "quality": round(zone_quality_score(z), 2),
    }
    if current_price and out["top"] is not None:
        mid = (out["top"] + out["bottom"]) / 2
        out["dist_from_price"] = round(abs(mid - current_price), 2)
        out["side"] = "above" if mid > current_price else ("below" if mid < current_price else "at")
    return out


def build_zone_map(zones_by_tf, current_price, limit=12):
    """
    Flat list of zones for TOS markup — nearest high-quality boxes first.
    """
    all_z = []
    for tf, zs in (zones_by_tf or {}).items():
        for z in zs:
            if z.get("mitigated"):
                continue
            s = serialize_zone(z, current_price)
            if s:
                all_z.append(s)
    all_z.sort(
        key=lambda z: (
            0 if z.get("fresh") else 1,
            z.get("dist_from_price") if z.get("dist_from_price") is not None else 1e9,
            -z.get("quality", 0),
        )
    )
    return all_z[:limit]


def pick_entry_zone_candidates(zones_ltf, zones_htf, current_price):
    """Nearest fresh/partial LTF zones with optional HTF stack boost."""
    demand, supply = find_nearest_zones(zones_ltf, current_price, allow_partial=True)

    def htf_boost(zone):
        if not zone:
            return False
        mid = (zone["top"] + zone["bottom"]) / 2
        tol = max((zone["top"] - zone["bottom"]) * 3, current_price * 0.003)
        for hz in zones_htf:
            if hz.get("type") != zone.get("type") or hz.get("mitigated"):
                continue
            hmid = (hz["top"] + hz["bottom"]) / 2
            if abs(mid - hmid) < tol:
                return True
        return False

    return demand, supply, htf_boost


def find_nearest_zones(zones, current_price, allow_partial=True):
    """Nearest tradeable demand below / supply above (fresh, or allowable partial)."""
    def ok(z):
        if z.get("mitigated"):
            return False
        if z.get("fresh"):
            return True
        return allow_partial and z.get("partial")

    demand = [z for z in zones if z["type"] == "demand" and z["top"] < current_price and ok(z)]
    supply = [z for z in zones if z["type"] == "supply" and z["bottom"] > current_price and ok(z)]

    nearest_demand = max(demand, key=lambda z: z["top"]) if demand else None
    nearest_supply = min(supply, key=lambda z: z["bottom"]) if supply else None
    return nearest_demand, nearest_supply


# ===================================================================
# VOLUME
# ===================================================================

def detect_volume_spikes(intraday_bars, zones, lookback=100):
    if len(intraday_bars) < 20:
        return {"avg_volume": 0, "recent_spikes": [], "zone_volumes": {}}

    bars = intraday_bars[-lookback:]
    volumes = [b["volume"] for b in bars if b.get("volume") is not None]
    if not volumes:
        return {"avg_volume": 0, "recent_spikes": [], "zone_volumes": {}}
    avg_volume = statistics.mean(volumes)

    recent_spikes = []
    for b in bars[-20:]:
        vol = b.get("volume") or 0
        if vol > avg_volume * 1.8:
            recent_spikes.append({
                "time": b.get("datetime"),
                "volume": vol,
                "ratio": vol / avg_volume if avg_volume else 0,
                "price": b["close"],
                "session": tag_bar_session(b),
            })

    zone_volume_map = {}
    for z in zones:
        idx = z["origin_index"]
        if idx < len(bars):
            origin_vol = bars[idx].get("volume") or 0
            zone_volume_map[id(z)] = {
                "origin_volume_ratio": origin_vol / avg_volume if avg_volume else 0,
                "origin_above_avg": origin_vol > avg_volume * 1.3,
            }

    return {
        "avg_volume": avg_volume,
        "recent_spikes": recent_spikes,
        "zone_volumes": zone_volume_map,
    }


# ===================================================================
# CONFLUENCE
# ===================================================================

def compute_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def compute_vwap(bars):
    total_pv = 0
    total_v = 0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        total_pv += typical * (b.get("volume") or 0)
        total_v += b.get("volume") or 0
    return total_pv / total_v if total_v else None


def prior_week_levels(daily_bars):
    if len(daily_bars) < 6:
        return None

    last_ms = daily_bars[-1].get("datetime", 0)
    if last_ms:
        last_date = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).date()
    else:
        last_date = now_et().date()

    days_since_monday = last_date.weekday()
    this_week_monday = last_date - timedelta(days=days_since_monday)

    prior_bars = [
        b for b in daily_bars
        if b.get("datetime") and
        datetime.fromtimestamp(b["datetime"] / 1000, tz=timezone.utc).date() < this_week_monday
    ]
    if not prior_bars:
        return None

    prior_bars = prior_bars[-5:]
    return {
        "high": max(b["high"] for b in prior_bars),
        "low": min(b["low"] for b in prior_bars),
        "close": prior_bars[-1]["close"],
    }


def detect_trend(daily_bars):
    if len(daily_bars) < 10:
        return "unclear"
    recent = daily_bars[-10:]
    highs = [b["high"] for b in recent]
    lows = [b["low"] for b in recent]
    h1_high = max(highs[:5]); h2_high = max(highs[5:])
    h1_low = min(lows[:5]); h2_low = min(lows[5:])
    if h2_high > h1_high and h2_low > h1_low:
        return "up"
    if h2_high < h1_high and h2_low < h1_low:
        return "down"
    return "range"


def confluence_check(zone, current_price, daily_bars, intraday_bars, premarket_pack=None):
    factors = []
    zone_mid = (zone["top"] + zone["bottom"]) / 2
    tolerance = max((zone["top"] - zone["bottom"]) * 1.5, current_price * 0.0015)

    pw = prior_week_levels(daily_bars)
    if pw:
        if abs(zone_mid - pw["high"]) < tolerance:
            factors.append("prior_week_high")
        if abs(zone_mid - pw["low"]) < tolerance:
            factors.append("prior_week_low")
        if abs(zone_mid - pw["close"]) < tolerance:
            factors.append("prior_week_close")

    daily_closes = [b["close"] for b in daily_bars]
    for period, name in [(20, "ema20"), (50, "ema50"), (200, "ema200")]:
        ema = compute_ema(daily_closes, period)
        if ema and abs(zone_mid - ema) < tolerance:
            factors.append(name)

    vwap = compute_vwap(intraday_bars[-20:])
    if vwap and abs(zone_mid - vwap) < tolerance:
        factors.append("vwap")

    nearest_round = round(zone_mid / 5) * 5
    if abs(zone_mid - nearest_round) < (zone["top"] - zone["bottom"] or 1):
        factors.append("round_number")

    # Premarket pack confluence
    if premarket_pack:
        for key, label in [
            ("overnight_high", "overnight_high"),
            ("overnight_low", "overnight_low"),
            ("premarket_high", "premarket_high"),
            ("premarket_low", "premarket_low"),
            ("globex_high", "globex_high"),
            ("globex_low", "globex_low"),
        ]:
            lvl = premarket_pack.get(key)
            if lvl is not None and abs(zone_mid - lvl) < tolerance:
                factors.append(label)
        pd = premarket_pack.get("prior_day") or {}
        for k, label in [("high", "prior_day_high"), ("low", "prior_day_low"), ("close", "prior_day_close")]:
            if pd.get(k) is not None and abs(zone_mid - pd[k]) < tolerance:
                factors.append(label)

    return factors


# ===================================================================
# MACRO — FLAG ONLY (no conviction kill)
# ===================================================================

def score_macro(macro_events, earnings_events, direction, hold_hours=4):
    """
    Hostile macro is a FLAG for the trader — it does not veto or heavily
    penalize setups (user preference). hold_hours default tightened to
    morning session (~open through Carmine cut).
    """
    high_impact = [e for e in macro_events if e.get("impact") == "high"]

    now = datetime.now(timezone.utc)
    hold_end = now.timestamp() + hold_hours * 3600
    risk_in_hold = []
    for e in high_impact:
        event_time = e.get("time")
        if event_time:
            try:
                t = datetime.fromisoformat(event_time.replace("Z", "+00:00")).timestamp()
                if now.timestamp() < t < hold_end:
                    risk_in_hold.append(e)
            except Exception:
                pass

    earnings_risk = len(earnings_events) > 0

    return {
        "high_impact_count": len(high_impact),
        "events_in_hold_period": risk_in_hold,
        "earnings_in_window": earnings_events,
        "hostile": len(risk_in_hold) > 0 or earnings_risk,
        "flag_only": True,
        "flag_message": (
            "Event risk into hold window — size down / be picky"
            if (len(risk_in_hold) > 0 or earnings_risk)
            else None
        ),
    }


# ===================================================================
# SETUP CONSTRUCTION
# ===================================================================

def build_setup(symbol, zone, current_price, confluence_factors, volume_info,
                macro_info, daily_bars, premarket_pack=None, htf_aligned=False,
                session_phase=None):
    """
    Build a tradeable setup. Rejects if R:R < 1.5 or zone mitigated.
    Stops at distal (+ small buffer). Entry language = playbook (tag proximal).
    """
    if zone.get("mitigated"):
        return None

    zone_height = max(zone["top"] - zone["bottom"], current_price * 0.0005)
    pattern = zone.get("pattern", "S/D")
    session_phase = session_phase or current_desk_phase()

    if zone["type"] == "demand":
        direction = "CALL"
        proximal = zone["proximal"]
        distal = zone["distal"]
        entry_trigger = (
            f"Tag proximal {proximal:.2f} in {pattern}; "
            f"confirm hold above distal {distal:.2f} (1–5m agency)"
        )
        stop = distal - zone_height * 0.15  # distal + small buffer beyond
        pw = prior_week_levels(daily_bars)
        # Target priority: opposing overnight/premarket level → prior week high
        target = None
        if premarket_pack:
            for k in ("premarket_high", "overnight_high", "globex_high"):
                lvl = premarket_pack.get(k)
                if lvl and lvl > current_price:
                    target = lvl
                    break
        if target is None:
            target = pw["high"] if pw else current_price + (current_price - stop) * 2.5
    else:
        direction = "PUT"
        proximal = zone["proximal"]
        distal = zone["distal"]
        entry_trigger = (
            f"Tag proximal {proximal:.2f} in {pattern}; "
            f"confirm rejection below distal {distal:.2f} (1–5m agency)"
        )
        stop = distal + zone_height * 0.15
        pw = prior_week_levels(daily_bars)
        target = None
        if premarket_pack:
            for k in ("premarket_low", "overnight_low", "globex_low"):
                lvl = premarket_pack.get(k)
                if lvl and lvl < current_price:
                    target = lvl
                    break
        if target is None:
            target = pw["low"] if pw else current_price - (stop - current_price) * 2.5

    # Risk measured from proximal (entry region), not current mid-air price when far
    entry_ref = proximal
    risk = abs(entry_ref - stop)
    reward = abs(target - entry_ref)
    rr = reward / risk if risk > 0 else 0

    if rr < 1.5:
        return None

    # Expiry: weekly (this/next Friday). 0DTE envelope set later when scoring A-tier.
    today = now_et().date()
    friday = next_friday(today)
    # If Friday already passed for calculated — next_friday handles weekday
    if friday < today:
        friday = today + timedelta(days=((4 - today.weekday()) % 7) or 7)
    target_expiry = friday.isoformat()

    vol_map = volume_info.get("zone_volumes", {}).get(id(zone), {})
    playbook_pass = (
        (zone.get("fresh") or zone.get("partial"))
        and not zone.get("mitigated")
        and zone.get("agency", 0) >= 1.4
    )

    return {
        "instrument": symbol,
        "direction": direction,
        "zone_top": zone["top"],
        "zone_bottom": zone["bottom"],
        "proximal": round(proximal, 2),
        "distal": round(distal, 2),
        "pattern": pattern,
        "playbook_label": pattern,
        "current_price": current_price,
        "entry_trigger": entry_trigger,
        "entry_ref": round(entry_ref, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_points": round(risk, 2),
        "reward_points": round(reward, 2),
        "rr_ratio": round(rr, 2),
        "target_expiry": target_expiry,
        "expiry_style": "weekly",  # may upgrade to 0dte if A-tier later
        "allow_0dte": False,
        "confluence_factors": confluence_factors,
        "zone_fresh": bool(zone.get("fresh")),
        "zone_partial": bool(zone.get("partial")),
        "zone_mitigated": bool(zone.get("mitigated")),
        "zone_strength": round(zone["strength"], 2),
        "agency": zone.get("agency", round(zone["strength"], 2)),
        "volume_origin_ratio": vol_map.get("origin_volume_ratio", 0),
        "htf_aligned": htf_aligned,
        "playbook_pass": playbook_pass,
        "session_phase": session_phase,
        "session_label": (
            "PREMARKET" if session_phase == "premarket"
            else "RTH" if session_phase in ("rth_session", "post_session")
            else session_phase.upper()
        ),
        "entry_allowed_now": session_phase in ("premarket", "rth_session"),
    }


def score_conviction(setup, macro_info, volume_info):
    """
    Factor score 0–10. Hostile macro is FLAG ONLY — no −3 kill.
    """
    score = 0
    breakdown = {}

    if setup["zone_fresh"]:
        score += 2
        breakdown["zone_freshness"] = "Fresh zone (+2)"
    elif setup.get("zone_partial"):
        score += 1
        breakdown["zone_freshness"] = "Partial fill only (+1)"
    else:
        breakdown["zone_freshness"] = "Tested / weak (+0)"

    if setup["zone_strength"] >= 2.0:
        score += 2
        breakdown["zone_strength"] = f"Strong agency {setup['zone_strength']}x (+2)"
    elif setup["zone_strength"] >= 1.5:
        score += 1
        breakdown["zone_strength"] = f"Moderate agency {setup['zone_strength']}x (+1)"
    else:
        breakdown["zone_strength"] = f"Weak leave {setup['zone_strength']}x (+0)"

    vol_ratio = setup["volume_origin_ratio"]
    if vol_ratio >= 1.8:
        score += 2
        breakdown["volume"] = f"Strong origin volume ({vol_ratio:.1f}x) (+2)"
    elif vol_ratio >= 1.3:
        score += 1
        breakdown["volume"] = f"Moderate origin volume ({vol_ratio:.1f}x) (+1)"
    else:
        breakdown["volume"] = f"Thin origin volume ({vol_ratio:.1f}x) (+0)"

    conf_count = len(setup["confluence_factors"])
    if conf_count >= 3:
        score += 2
        breakdown["confluence"] = f"{conf_count} levels align (+2)"
    elif conf_count >= 1:
        score += 1
        breakdown["confluence"] = f"{conf_count} level(s) align (+1)"
    else:
        breakdown["confluence"] = "No HTF/level confluence (+0)"

    if setup["rr_ratio"] >= 2.5:
        score += 1
        breakdown["rr"] = f"R:R 1:{setup['rr_ratio']} (+1)"
    else:
        breakdown["rr"] = f"R:R 1:{setup['rr_ratio']} (+0)"

    if setup.get("htf_aligned"):
        score += 1
        breakdown["htf"] = "HTF structure aligned (+1)"
    else:
        breakdown["htf"] = "HTF not clearly aligned (+0)"

    # Macro: flag only
    if macro_info.get("hostile"):
        breakdown["macro"] = "FLAG: event / earnings risk in morning window (not a veto)"
    else:
        breakdown["macro"] = "No major morning event risk flagged"

    # Pattern clarity bonus is already in agency; cap
    score = max(0, min(10, score))

    if score >= 8:
        label = "High"
    elif score >= 5:
        label = "Medium"
    else:
        label = "Low"

    return score, label, breakdown


def apply_option_style(setup):
    """Weekly default; enable 0DTE only for A-tier."""
    a_tier = envelope_for_0dte_eligible(
        setup.get("conviction_score", 0),
        setup.get("zone_fresh", False),
        setup.get("playbook_pass", False),
    )
    setup["allow_0dte"] = a_tier
    setup["expiry_style"] = "0dte_eligible" if a_tier else "weekly"
    if a_tier:
        setup["target_expiry_0dte"] = now_et().date().isoformat()
        setup["option_note"] = (
            "A-tier: weekly is default. 0DTE allowed ONLY if spread/liquidity gates pass."
        )
    else:
        setup["target_expiry_0dte"] = None
        setup["option_note"] = "Weekly options preferred. 0DTE locked (not A-tier)."
    return setup


def strict_playbook_filter(setup) -> Tuple[bool, str]:
    """
    Strict Carmine-style filter. Returns (pass, reason).
    """
    if not setup:
        return False, "no_setup"
    if setup.get("zone_mitigated"):
        return False, "zone_mitigated"
    if not (setup.get("zone_fresh") or setup.get("zone_partial")):
        return False, "not_fresh"
    if setup.get("agency", 0) < 1.4:
        return False, "weak_agency"
    if setup.get("rr_ratio", 0) < 1.5:
        return False, "rr_below_1.5"
    if setup.get("conviction_score", 0) < 5:
        return False, "conviction_below_medium"
    # HTF soft preference: if trend clearly opposes, reject
    # (caller attaches trend_align if needed)
    if setup.get("trend_opposes"):
        return False, "against_daily_trend"
    if not setup.get("entry_allowed_now") and setup.get("session_phase") in (
        "post_session", "afterhours", "closed", "overnight"
    ):
        return False, "outside_session_window"
    return True, "pass"


# ===================================================================
# TOP-LEVEL PIPELINE
# ===================================================================

def analyze_instrument(
    symbol,
    daily_bars,
    intraday_bars,
    quote,
    macro_events,
    earnings_events,
    bars_5m=None,
    bars_15m=None,
    bars_1h=None,
):
    # Resolve multi-TF sources (fallback to whatever we got)
    bars_15m = bars_15m or intraday_bars or []
    bars_5m = bars_5m or []
    bars_1h = bars_1h or []
    # Prefer true 1H series; else aggregate from 15m
    if len(bars_1h) < 20 and len(bars_15m) >= 40:
        bars_1h = aggregate_bars(bars_15m, 4)

    # Use finest available for pack price feed; extended hours from 15m/5m
    pack_bars = bars_5m if len(bars_5m) >= 20 else bars_15m
    current_price = quote.get("quote", {}).get("lastPrice") or (
        pack_bars[-1]["close"] if pack_bars else 0
    )
    if not current_price and quote.get("quote"):
        q = quote["quote"]
        current_price = q.get("mark") or q.get("extendedHoursPrice") or 0

    phase = current_desk_phase()

    if not current_price or len(pack_bars) < 20 or len(daily_bars) < 10:
        return {
            "symbol": symbol,
            "error": "Insufficient data",
            "setup": None,
            "premarket_pack": None,
            "session_phase": phase,
            "zone_map": [],
            "zones_by_tf": {},
        }

    premarket_pack = build_premarket_pack(pack_bars, daily_bars, current_price)

    zones_5m = detect_zones(bars_5m, lookback_bars=160, timeframe="5m") if len(bars_5m) >= 20 else []
    zones_15m = detect_zones(bars_15m, lookback_bars=120, timeframe="15m") if len(bars_15m) >= 20 else []
    zones_1h = detect_zones(bars_1h, lookback_bars=80, timeframe="1H") if len(bars_1h) >= 20 else []
    zones_d = detect_htf_zones(daily_bars)

    zones_by_tf = {
        "5m": zones_5m,
        "15m": zones_15m,
        "1H": zones_1h,
        "D": zones_d,
    }
    zone_map = build_zone_map(zones_by_tf, current_price, limit=14)

    # Entry TF preference: 15m primary, fall back 5m
    entry_zones = zones_15m if zones_15m else zones_5m
    htf_for_align = zones_1h + zones_d
    volume_info = detect_volume_spikes(bars_15m if bars_15m else pack_bars, entry_zones)
    nearest_demand, nearest_supply = find_nearest_zones(entry_zones, current_price)
    trend = detect_trend(daily_bars)
    pw_levels = prior_week_levels(daily_bars)

    def htf_ok(zone):
        if not zone:
            return False
        mid = (zone["top"] + zone["bottom"]) / 2
        tol = max((zone["top"] - zone["bottom"]) * 3, current_price * 0.003)
        for hz in htf_for_align:
            if hz.get("type") != zone.get("type") or hz.get("mitigated"):
                continue
            hmid = (hz["top"] + hz["bottom"]) / 2
            if abs(mid - hmid) < tol:
                return True
        if zone["type"] == "demand" and trend == "up":
            return True
        if zone["type"] == "supply" and trend == "down":
            return True
        return False

    candidates = []
    reject_log = []
    for zone in filter(None, [nearest_demand, nearest_supply]):
        confluence = confluence_check(
            zone, current_price, daily_bars, bars_15m if bars_15m else pack_bars, premarket_pack
        )
        # stack confluence tag
        if htf_ok(zone) and "htf_stack" not in confluence:
            confluence = list(confluence) + ["htf_stack"]
        setup = build_setup(
            symbol, zone, current_price, confluence, volume_info, None,
            daily_bars, premarket_pack=premarket_pack,
            htf_aligned=htf_ok(zone), session_phase=phase,
        )
        if not setup:
            reject_log.append({
                "pattern": zone.get("pattern"),
                "type": zone["type"],
                "timeframe": zone.get("timeframe"),
                "reason": "rr_or_mitigated",
            })
            continue

        if trend == "up" and setup["direction"] == "PUT":
            setup["trend_opposes"] = True
        elif trend == "down" and setup["direction"] == "CALL":
            setup["trend_opposes"] = True
        else:
            setup["trend_opposes"] = False

        setup["zone_timeframe"] = zone.get("timeframe")
        setup["tos_draw"] = zone.get("tos_draw")

        macro_info = score_macro(macro_events, earnings_events, setup["direction"])
        setup["macro"] = macro_info
        score, label, breakdown = score_conviction(setup, macro_info, volume_info)
        setup["conviction_score"] = score
        setup["conviction_label"] = label
        setup["conviction_breakdown"] = breakdown
        apply_option_style(setup)

        ok, reason = strict_playbook_filter(setup)
        setup["strict_pass"] = ok
        setup["strict_reason"] = reason
        if not ok:
            reject_log.append({
                "pattern": setup.get("pattern"),
                "direction": setup["direction"],
                "timeframe": setup.get("zone_timeframe"),
                "reason": reason,
                "score": score,
            })
            continue
        candidates.append(setup)

    best = max(candidates, key=lambda s: s["conviction_score"]) if candidates else None

    # Orderflow PROXY (OHLC volume — not true footprint)
    try:
        from orderflow_proxy import build_orderflow_proxy
        of_bars = bars_5m if len(bars_5m) >= 20 else pack_bars
        of_zones = zone_map or []
        orderflow = build_orderflow_proxy(
            of_bars,
            zones=of_zones,
            current_price=current_price,
            timeframe="5m" if len(bars_5m) >= 20 else "15m",
        )
        # Soft annotate best setup only — never invent tape proof as hard gate
        if best and orderflow.get("available"):
            best["orderflow_proxy"] = {
                "confirmation": orderflow.get("confirmation"),
                "rvol": (orderflow.get("relative_volume") or {}).get("rvol"),
                "rvol_label": (orderflow.get("relative_volume") or {}).get("label"),
                "imbalance_bias": (orderflow.get("imbalance_proxy") or {}).get("bias"),
                "poc": (orderflow.get("volume_profile") or {}).get("poc"),
                "narrative": orderflow.get("narrative"),
            }
            conf = orderflow.get("confirmation")
            if conf == "supports_long_at_demand" and best.get("direction") == "CALL":
                best.setdefault("conviction_breakdown", {})["orderflow_proxy"] = (
                    "Proxy supports long (RVOL/imbalance) — soft only"
                )
            elif conf == "supports_short_at_supply" and best.get("direction") == "PUT":
                best.setdefault("conviction_breakdown", {})["orderflow_proxy"] = (
                    "Proxy supports short (RVOL/imbalance) — soft only"
                )
            elif conf == "thin_liquidity_caution":
                best.setdefault("conviction_breakdown", {})["orderflow_proxy"] = (
                    "FLAG: thin RVOL proxy — size down"
                )
            else:
                best.setdefault("conviction_breakdown", {})["orderflow_proxy"] = (
                    f"Proxy read: {conf}"
                )
    except Exception as e:
        orderflow = {
            "available": False,
            "disclaimer": f"orderflow proxy failed: {e}",
        }

    return {
        "symbol": symbol,
        "current_price": current_price,
        "trend": trend,
        "prior_week_levels": pw_levels,
        "premarket_pack": premarket_pack,
        "session_phase": phase,
        "zones_detected": len(entry_zones),
        "htf_zones_detected": len(htf_for_align),
        "zones_by_tf_counts": {k: len(v) for k, v in zones_by_tf.items()},
        "zone_map": zone_map,
        "nearest_demand": serialize_zone(nearest_demand, current_price),
        "nearest_supply": serialize_zone(nearest_supply, current_price),
        "recent_volume_spikes": volume_info["recent_spikes"],
        "orderflow_proxy": orderflow,
        "setup": best,
        "all_candidates": candidates,
        "reject_log": reject_log,
        "chart_note": (
            "Zones from Schwab Trader API candles (same family as TOS charts). "
            "Use zone_map to draw boxes on TOS 5m/15m/1H/D. Extended-hours feed "
            "can differ slightly from EXTO chart styles on TOS."
        ),
    }


def rank_setups(instrument_analyses, max_recs=2):
    """
    Rank strict-pass setups. Top max_recs become the day's recommendations.
    Instruments without setups still returned for levels display.
    """
    with_setup = [a for a in instrument_analyses if a.get("setup")]
    without = [a for a in instrument_analyses if not a.get("setup")]
    ranked = sorted(
        with_setup,
        key=lambda a: a["setup"]["conviction_score"],
        reverse=True,
    )
    # Cap printed recs deep-copy flag
    for i, a in enumerate(ranked):
        a["recommended"] = i < max_recs
    for a in without:
        a["recommended"] = False
    return ranked + without


# ===================================================================
# PLAN DENSITY — full morning card even when 0 entries
# ===================================================================

def _fmt_lvl(x):
    if x is None:
        return None
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def key_levels_ranked(analysis, limit=8):
    """Flatten pack + HTF + nearest zones into ranked levels nearest price."""
    price = analysis.get("current_price") or 0
    pack = analysis.get("premarket_pack") or {}
    levels = []

    def add(name, val, kind="level"):
        v = _fmt_lvl(val)
        if v is None or not price:
            return
        levels.append({
            "name": name,
            "price": v,
            "kind": kind,
            "dist": round(abs(v - price), 2),
            "side": "above" if v > price else ("below" if v < price else "at"),
        })

    pd = pack.get("prior_day") or {}
    for k, n in [
        ("overnight_high", "ONh"),
        ("overnight_low", "ONl"),
        ("overnight_mid", "ON mid"),
        ("premarket_high", "PMh"),
        ("premarket_low", "PMl"),
        ("globex_high", "Globex H"),
        ("globex_low", "Globex L"),
    ]:
        add(n, pack.get(k), "session")
    for k, n in [("high", "Prior day H"), ("low", "Prior day L"), ("close", "Prior day C")]:
        add(n, pd.get(k), "prior_day")

    pw = analysis.get("prior_week_levels") or {}
    for k, n in [("high", "Prior week H"), ("low", "Prior week L"), ("close", "Prior week C")]:
        add(n, pw.get(k), "weekly")

    for zone, label in [
        (analysis.get("nearest_demand"), "Nearest demand"),
        (analysis.get("nearest_supply"), "Nearest supply"),
    ]:
        if zone:
            mid = None
            if isinstance(zone, dict) and zone.get("top") is not None:
                mid = (zone["top"] + zone["bottom"]) / 2
                pat = zone.get("pattern", "S/D")
                tf = zone.get("timeframe") or ""
                add(f"{label} {tf} ({pat})".strip(), mid, "zone")

    # From multi-TF zone map if present
    for z in (analysis.get("zone_map") or [])[:6]:
        if z.get("top") is None:
            continue
        mid = (z["top"] + z["bottom"]) / 2
        add(
            f"{z.get('timeframe','')} {z.get('pattern','')} {z.get('type','')}".strip(),
            mid,
            "zone_map",
        )

    # dedupe near-identical prices
    levels.sort(key=lambda L: L["dist"])
    out = []
    seen = []
    for L in levels:
        if any(abs(L["price"] - s) < max(price * 0.0003, 0.05) for s in seen):
            continue
        seen.append(L["price"])
        out.append(L)
        if len(out) >= limit:
            break
    return out


def instrument_bias_narrative(analysis):
    """Plain-language Carmine-session bias for one symbol."""
    sym = analysis.get("symbol", "?")
    price = analysis.get("current_price")
    pack = analysis.get("premarket_pack") or {}
    trend = analysis.get("trend", "unclear")
    notes = list(pack.get("bias_notes") or [])
    gap_dir = pack.get("gap_direction", "flat")
    gap_pts = pack.get("gap_points")
    phase = analysis.get("session_phase") or current_desk_phase()

    parts = []
    if price:
        parts.append(f"{sym} last {_fmt_lvl(price)}.")
    if gap_pts is not None:
        parts.append(
            f"Gap {gap_dir} {gap_pts:+.2f} pts vs prior close."
            if gap_pts
            else f"Flat near prior close ({gap_dir})."
        )
    if "price_above_overnight_mid" in notes:
        parts.append("Holding above overnight mid — bulls own the overnight auction for now.")
    elif "price_below_overnight_mid" in notes:
        parts.append("Below overnight mid — overnight auction leans defensive.")
    if "at_or_near_premarket_high" in notes:
        parts.append("At/near premarket high — break/hold decides open momentum.")
    if "at_or_near_premarket_low" in notes:
        parts.append("At/near premarket low — reclaim required for bull interest.")

    if trend == "up":
        parts.append("Daily structure: up — prefer DBR longs, short only A-tier RBD with cash.")
    elif trend == "down":
        parts.append("Daily structure: down — prefer RBD shorts, long only A-tier DBR surface.")
    else:
        parts.append("Daily structure: range / mixed — wait for agency at a level, no mid-range chop.")

    if phase == "overnight":
        parts.append("Outside entry window (overnight) — map only; no option entries printed.")
    elif phase == "premarket":
        parts.append("Premarket live — option entries only if playbook + liquidity gates pass.")
    elif phase == "rth_session":
        parts.append("RTH session (open → ~11:30 ET) — primary execution window.")
    elif phase == "post_session":
        parts.append("Past Carmine cut — no new entries; manage or flat.")

    return " ".join(parts)


def instrument_watch_list(analysis):
    """Conditional frameworks even when strict_pass is false."""
    watch = []
    phase = analysis.get("session_phase") or current_desk_phase()
    allow_entries = phase in ("premarket", "rth_session")

    for zone, side in [
        (analysis.get("nearest_demand"), "CALL"),
        (analysis.get("nearest_supply"), "PUT"),
    ]:
        if not zone or zone.get("mitigated"):
            continue
        pattern = zone.get("pattern", "S/D")
        proxim = _fmt_lvl(zone.get("proximal") or (
            zone["top"] if zone["type"] == "demand" else zone["bottom"]
        ))
        distal = _fmt_lvl(zone.get("distal") or (
            zone["bottom"] if zone["type"] == "demand" else zone["top"]
        ))
        status = "fresh" if zone.get("fresh") else (
            "partial" if zone.get("partial") else "tested"
        )
        agency = zone.get("agency") or zone.get("strength")
        watch.append({
            "side": side,
            "pattern": pattern,
            "status": status,
            "proximal": proxim,
            "distal": distal,
            "agency": agency,
            "condition": (
                f"IF price tags {proxim} with hold above distal {distal} and clean agency "
                f"({'eligible now' if allow_entries else 'wait for entry window'}) "
                f"THEN weekly {side} framework — not a live rec until gates print."
            ),
            "actionable_now": bool(allow_entries and zone.get("fresh") and (agency or 0) >= 1.4),
        })

    for r in analysis.get("reject_log") or []:
        watch.append({
            "side": r.get("direction") or r.get("type") or "—",
            "pattern": r.get("pattern", "—"),
            "status": "rejected",
            "proximal": None,
            "distal": None,
            "agency": None,
            "condition": (
                f"Blocked by strict filter: {r.get('reason')}"
                + (f" (score {r.get('score')})" if r.get("score") is not None else "")
                + "."
            ),
            "actionable_now": False,
        })

    return watch[:6]


def why_no_trade(analysis, has_live_setup):
    reasons = []
    phase = analysis.get("session_phase") or current_desk_phase()
    if has_live_setup:
        return ["Live option entry printed for this symbol."]
    if phase in ("overnight", "afterhours", "closed", "post_session"):
        reasons.append(f"Session phase is **{phase}** — outside premkt/RTH entry window.")
    if analysis.get("error"):
        reasons.append(f"Data issue: {analysis['error']}")
    if not analysis.get("zones_detected"):
        reasons.append("No qualifying LTF S/D bases on the current lookback.")
    rejects = analysis.get("reject_log") or []
    for r in rejects[:4]:
        reasons.append(
            f"{r.get('pattern') or r.get('direction') or 'setup'}: {r.get('reason')}"
            + (f" (score {r['score']})" if r.get("score") is not None else "")
        )
    if not reasons:
        reasons.append("No setup cleared playbook + R:R + liquidity — no trade is correct.")
    return reasons


def build_instrument_plan(analysis):
    has_setup = bool(analysis.get("setup") and analysis.get("recommended"))
    return {
        "symbol": analysis.get("symbol"),
        "current_price": analysis.get("current_price"),
        "trend": analysis.get("trend"),
        "session_phase": analysis.get("session_phase"),
        "bias": instrument_bias_narrative(analysis),
        "key_levels": key_levels_ranked(analysis),
        "watch_list": instrument_watch_list(analysis),
        "why_no_trade": why_no_trade(analysis, has_setup),
        "has_live_entry": has_setup,
        "zones_detected": analysis.get("zones_detected"),
        "htf_zones_detected": analysis.get("htf_zones_detected"),
        "reject_log": analysis.get("reject_log") or [],
        "premarket_pack_summary": {
            "gap_direction": (analysis.get("premarket_pack") or {}).get("gap_direction"),
            "gap_points": (analysis.get("premarket_pack") or {}).get("gap_points"),
            "overnight_high": (analysis.get("premarket_pack") or {}).get("overnight_high"),
            "overnight_low": (analysis.get("premarket_pack") or {}).get("overnight_low"),
            "premarket_high": (analysis.get("premarket_pack") or {}).get("premarket_high"),
            "premarket_low": (analysis.get("premarket_pack") or {}).get("premarket_low"),
        },
    }


def build_morning_plan(instrument_analyses, recommendations, phase, macro_summary=None):
    """
    Full desk plan — always returned, even with 0 option prints.
    """
    phase = phase or current_desk_phase()
    plans = [build_instrument_plan(a) for a in instrument_analyses if a.get("symbol")]

    # Composite market bias from SPY first, then QQQ
    spy = next((p for p in plans if p["symbol"] == "SPY"), plans[0] if plans else None)
    qqq = next((p for p in plans if p["symbol"] == "QQQ"), None)

    headline_bits = []
    if phase == "overnight":
        headline_bits.append("OVERNIGHT MAP ONLY")
    elif phase == "premarket":
        headline_bits.append("PREMARKET PLAN LIVE")
    elif phase == "rth_session":
        headline_bits.append("RTH EXECUTION WINDOW")
    else:
        headline_bits.append(f"PHASE {phase.upper()}")

    n_live = len(recommendations or [])
    if n_live:
        headline_bits.append(f"{n_live} option entr{'y' if n_live == 1 else 'ies'}")
    else:
        headline_bits.append("NO LIVE OPTION ENTRY")

    if spy:
        g = (spy.get("premarket_pack_summary") or {}).get("gap_direction")
        if g and g != "flat":
            headline_bits.append(f"SPY gap {g}")

    # Action list
    actions = []
    if phase in ("overnight", "afterhours"):
        actions.append("Build levels & watches only — do not force an option print overnight.")
    if phase == "premarket":
        actions.append("Hunt tags for watchlist IF→THEN frameworks; size only on printed live entries.")
    if phase == "rth_session":
        actions.append("Primary session: take live prints only, flat by ~11:30 ET / 8:30 AM PT.")
    if n_live == 0:
        actions.append("No trade is success-mode today until a watch condition + gates fire.")
    for p in plans:
        for w in p.get("watch_list") or []:
            if w.get("actionable_now"):
                actions.append(
                    f"{p['symbol']}: watch ready — {w['side']} {w['pattern']} at {w['proximal']}"
                )
    if not actions:
        actions.append("Stand down. Remap on next scan.")

    # Mega-cap earnings flags if present
    macro_flags = []
    if macro_summary:
        for e in (macro_summary.get("high_impact_events") or [])[:4]:
            macro_flags.append(f"High impact: {e.get('event', 'event')}")
        for e in (macro_summary.get("earnings_today") or [])[:4]:
            macro_flags.append(f"Earnings: {e.get('symbol')}")
        if macro_summary.get("note"):
            macro_flags.append(macro_summary["note"])

    grade = "A" if n_live >= 2 else ("B" if n_live == 1 else "C")
    if phase not in ("premarket", "rth_session") and n_live == 0:
        grade = "MAP"  # map-only daily grade

    return {
        "headline": " · ".join(headline_bits),
        "grade": grade,
        "session_phase": phase,
        "clock": {
            "et": now_et().strftime("%Y-%m-%d %H:%M %Z"),
            "pt": now_pt().strftime("%Y-%m-%d %H:%M %Z"),
        },
        "market_bias": spy["bias"] if spy else "Insufficient data.",
        "secondary_bias": qqq["bias"] if qqq else None,
        "instruments": plans,
        "actions": actions[:8],
        "live_entries": n_live,
        "macro_flags": macro_flags,
        "playbook_reminder": (
            "Carmine rules: map HTF → wait for LTF tag at proximal → confirm agency → "
            "weekly options default, 0DTE only A-tier → flat by session cut. "
            "Hostile macro is a flag, not a hard veto."
        ),
    }


def build_prompt_cowboy_board(morning_plan, phase=None):
    """
    Prompt Cowboy integration: ready-to-paste strong prompts so the user
    can ask Hermes better follow-ups from this scan.
    """
    phase = phase or (morning_plan or {}).get("session_phase") or current_desk_phase()
    hp = morning_plan or {}
    inst_lines = []
    for p in hp.get("instruments") or []:
        levels = ", ".join(
            f"{L['name']} {L['price']}" for L in (p.get("key_levels") or [])[:4]
        )
        inst_lines.append(
            f"- {p.get('symbol')}: trend={p.get('trend')}, live_entry={p.get('has_live_entry')}, "
            f"zones={p.get('zones_detected')}/{p.get('htf_zones_detected')} htf, levels: {levels or 'n/a'}"
        )
    context_blob = "\n".join([
        f"Session phase: {phase}",
        f"Headline: {hp.get('headline')}",
        f"Grade: {hp.get('grade')}",
        f"Actions: {'; '.join(hp.get('actions') or [])}",
        "Instruments:",
        *(inst_lines or ["- none"]),
    ])

    prompts = [
        {
            "id": "improve_plan",
            "title": "Tighten this morning plan",
            "use_when": "You want Hermes to rewrite the plan like Carmine coach notes",
            "prompt": f"""# ROLE
You are a Carmine Rosato / InvestiTrade-style premarket coach for a retail SPY/QQQ weekly-options trader in PST.

# OBJECTIVE
Rewrite the following desk scan into a crisp 6-bullet morning plan (bias → key levels → one A+ watch → invalidation → option style → flat time). Do not invent orderflow.

# CONTEXT
{context_blob}

# CONSTRAINTS
- User trades weeklies; 0DTE only if A-tier
- Premarket option entries allowed with liquidity gates
- Hostile macro = flag only
- No-trade is success when gates fail
- No fabricated win probabilities

# OUTPUT FORMAT
- Bias (2 lines)
- Levels board (bullets)
- Watches IF→THEN
- Live entry or explicit NO TRADE + why
- Risk reminders
""",
        },
        {
            "id": "zone_compare",
            "title": "Match zones to my chart",
            "use_when": "Your TOS levels don't match the desk",
            "prompt": f"""# ROLE
You are debugging S/D zone quality for a Carmine-style SPY/QQQ desk.

# OBJECTIVE
Given this scan, propose concrete rule changes so LTF zones match TOR/Thinkorswim eyes more often. Prefer multi-TF (daily/1H/15m/5m), RBD/DBR definition, fresh vs mitigated.

# CONTEXT
{context_blob}

# OUTPUT
1. Top 5 mismatches risk today
2. Exact detector rule tweaks (bullet, file-aware if possible)
3. Acceptance tests: 'when I scan Mon premkt, I should see X'
""",
        },
        {
            "id": "next_feature",
            "title": "Ship the next feature",
            "use_when": "Ask Hermes/Claude Code for the next build slice",
            "prompt": f"""# ROLE
You are implementing Pre-Market Trader at C:/Users/Welcome/Documents/VibeProjects/premarket-trader.

# OBJECTIVE
Propose and (if I say go) implement the next highest-value slice after plan density.

# CONTEXT
{context_blob}
Already shipped: multi-TF skeleton, premkt pack, strict filter, weekly options, premkt ticks, plan density, prompt-cowboy board.
Priority remaining: better chart zones, Discord weekday brief, live zone alerts, orderflow proxy, journal.

# OUTPUT
- Ranked next 3 tasks with effort/impact
- Recommended one task with acceptance criteria
- Files likely touched
""",
        },
        {
            "id": "discord_brief",
            "title": "Draft Discord morning post",
            "use_when": "You want a copy-paste brief for #overview",
            "prompt": f"""# ROLE
You write short Discord premarket briefs for Leashless Labs #overview (Pre-Market Research Desk).

# OBJECTIVE
Turn this scan into a 10–15 line Discord message. Plain trader language. No fake certainty.

# CONTEXT
{context_blob}

# FORMAT
**Premarket desk · PT date/time**
Bias
Key levels SPY / QQQ
Live entries or NO TRADE + why
Watches
Reminder: weeklies default · flat ~8:30 AM PT
""",
        },
    ]

    return {
        "method": "Prompt Cowboy (Hermes-native) — rough idea → strong structured prompt",
        "tip": (
            "Copy any prompt below into Discord with Hermes, or say "
            "`cowboy this: <your rough idea>` and I'll Q&A into a strong prompt first."
        ),
        "web_hybrid": "https://www.promptcowboy.ai/ (optional UI; paste result back here)",
        "prompts": prompts,
        "rough_starters": [
            "cowboy this: make zones match my TOS for SPY DBR longs",
            "cowboy this: Discord auto brief every weekday 4:45am PT",
            "cowboy this: live alert when price tags ONl with agency",
            "cowboy this: volume profile proxy for Carmine confirmation",
        ],
    }
