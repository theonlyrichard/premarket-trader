"""
Analysis engine.

Implements the full pipeline:
  1. Supply & demand zone detection from intraday bars
  2. Volume spike detection
  3. Technical confluence (trend, EMAs, prior week levels, VWAP, gaps)
  4. Macro alignment scoring
  5. Setup construction (entry, stop, target, R:R)
  6. Factor-based ranking

All outputs are deterministic — given the same data, you get the same setups.
No probability fabrication. Conviction is labeled High/Medium/Low based on
how many factors converge.
"""

import statistics
from datetime import datetime, timedelta, timezone


# ===================================================================
# ZONE DETECTION
# ===================================================================

def detect_zones(intraday_bars, lookback_bars=100):
    """
    Detect supply and demand zones from intraday bars.

    A demand zone is identified by:
      - A "base" of 1-3 consolidation candles (narrow range)
      - Followed by an explosive rally (wide range, strong close)
      - The zone is the low-to-high of the base

    A supply zone is the mirror image (base, then explosive drop).

    Returns list of zones with: type, top, bottom, origin_index, strength,
    freshness (fresh = never revisited since origin).
    """
    if len(intraday_bars) < 20:
        return []

    bars = intraday_bars[-lookback_bars:]
    ranges = [b["high"] - b["low"] for b in bars]
    avg_range = statistics.mean(ranges) if ranges else 0

    zones = []
    i = 2
    while i < len(bars) - 1:
        curr = bars[i]
        curr_range = curr["high"] - curr["low"]
        curr_body = abs(curr["close"] - curr["open"])

        # Explosive candle: range >= average AND body > 50% of range
        is_explosive = curr_range > avg_range * 1.0 and curr_body > curr_range * 0.5

        if is_explosive:
            # Look back 1-3 bars for a base (narrow range consolidation)
            base_start = max(0, i - 3)
            base_bars = bars[base_start:i]
            base_ranges = [b["high"] - b["low"] for b in base_bars]
            avg_base_range = statistics.mean(base_ranges) if base_ranges else 0

            if avg_base_range < avg_range * 0.9:  # base is tight-ish
                zone_top = max(b["high"] for b in base_bars)
                zone_bottom = min(b["low"] for b in base_bars)
                direction = "demand" if curr["close"] > curr["open"] else "supply"

                # Classify zone status by what subsequent bars do:
                #   fresh  — price has never returned to the zone
                #   tested — price entered the zone but close never went beyond it
                #   broken — a close went past the zone's far boundary (support/resistance broke)
                # Only fresh and tested zones qualify as setups.
                status = "fresh"
                for j in range(i + 1, len(bars)):
                    b = bars[j]
                    if direction == "demand":
                        if b["close"] < zone_bottom:
                            # Close went below support — zone is broken
                            status = "broken"
                            break
                        elif b["low"] <= zone_top and status == "fresh":
                            # Price entered the zone but hasn't broken it yet — tested
                            status = "tested"
                    else:  # supply
                        if b["close"] > zone_top:
                            # Close went above resistance — zone is broken
                            status = "broken"
                            break
                        elif b["high"] >= zone_bottom and status == "fresh":
                            # Price entered the zone but hasn't broken it yet — tested
                            status = "tested"

                zones.append({
                    "type": direction,
                    "top": zone_top,
                    "bottom": zone_bottom,
                    "origin_index": i,
                    "origin_time": curr.get("datetime"),
                    "strength": curr_range / avg_range if avg_range else 1.0,
                    "status": status,
                    "fresh": status == "fresh",  # kept for backward compat
                })
                i += 1
        i += 1

    return zones


def find_nearest_zones(zones, current_price):
    """Return the nearest qualifying demand below and supply above.
    Fresh and tested zones both qualify; broken zones are excluded."""
    demand = [z for z in zones if z["type"] == "demand" and z["top"] < current_price and z.get("status", "fresh") != "broken"]
    supply = [z for z in zones if z["type"] == "supply" and z["bottom"] > current_price and z.get("status", "fresh") != "broken"]

    nearest_demand = max(demand, key=lambda z: z["top"]) if demand else None
    nearest_supply = min(supply, key=lambda z: z["bottom"]) if supply else None
    return nearest_demand, nearest_supply


# ===================================================================
# VOLUME SPIKE DETECTION
# ===================================================================

def detect_volume_spikes(intraday_bars, zones, lookback=100):
    """
    For each zone, check if it was formed on above-average volume
    and flag any recent volume anomalies.
    """
    if len(intraday_bars) < 20:
        return {}

    bars = intraday_bars[-lookback:]
    volumes = [b["volume"] for b in bars]
    avg_volume = statistics.mean(volumes)

    recent_spikes = []
    for b in bars[-20:]:
        if b["volume"] > avg_volume * 1.8:
            recent_spikes.append({
                "time": b.get("datetime"),
                "volume": b["volume"],
                "ratio": b["volume"] / avg_volume if avg_volume else 0,
                "price": b["close"]
            })

    zone_volume_map = {}
    for z in zones:
        idx = z["origin_index"]
        if idx < len(bars):
            origin_vol = bars[idx]["volume"]
            zone_volume_map[id(z)] = {
                "origin_volume_ratio": origin_vol / avg_volume if avg_volume else 0,
                "origin_above_avg": origin_vol > avg_volume * 1.3
            }

    return {
        "avg_volume": avg_volume,
        "recent_spikes": recent_spikes,
        "zone_volumes": zone_volume_map
    }


# ===================================================================
# TECHNICAL CONFLUENCE
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
    """Session VWAP."""
    total_pv = 0
    total_v = 0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        total_pv += typical * b["volume"]
        total_v += b["volume"]
    return total_pv / total_v if total_v else None


def prior_week_levels(daily_bars):
    """Return prior week's high, low, close."""
    if len(daily_bars) < 6:
        return None

    last_ms = daily_bars[-1].get("datetime", 0)
    if last_ms:
        last_date = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).date()
    else:
        last_date = datetime.now(timezone.utc).date()

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
        "close": prior_bars[-1]["close"]
    }


def detect_trend(daily_bars):
    """Simple trend detection via higher highs/lows or lower highs/lows."""
    if len(daily_bars) < 10:
        return "unclear"
    recent = daily_bars[-10:]
    highs = [b["high"] for b in recent]
    lows = [b["low"] for b in recent]
    # Compare first half to second half
    h1_high = max(highs[:5]); h2_high = max(highs[5:])
    h1_low = min(lows[:5]); h2_low = min(lows[5:])
    if h2_high > h1_high and h2_low > h1_low:
        return "up"
    if h2_high < h1_high and h2_low < h1_low:
        return "down"
    return "range"


def confluence_check(zone, current_price, daily_bars, intraday_bars):
    """Check how many technical factors align with the zone."""
    factors = []

    # Prior week levels
    pw = prior_week_levels(daily_bars)
    if pw:
        zone_mid = (zone["top"] + zone["bottom"]) / 2
        tolerance = (zone["top"] - zone["bottom"]) * 1.5
        if abs(zone_mid - pw["high"]) < tolerance:
            factors.append("prior_week_high")
        if abs(zone_mid - pw["low"]) < tolerance:
            factors.append("prior_week_low")
        if abs(zone_mid - pw["close"]) < tolerance:
            factors.append("prior_week_close")

    # EMAs on daily
    daily_closes = [b["close"] for b in daily_bars]
    for period, name in [(20, "ema20"), (50, "ema50"), (200, "ema200")]:
        ema = compute_ema(daily_closes, period)
        if ema:
            zone_mid = (zone["top"] + zone["bottom"]) / 2
            tolerance = (zone["top"] - zone["bottom"]) * 1.5
            if abs(zone_mid - ema) < tolerance:
                factors.append(name)

    # VWAP (session)
    vwap = compute_vwap(intraday_bars[-20:])  # approximate session
    if vwap:
        zone_mid = (zone["top"] + zone["bottom"]) / 2
        tolerance = (zone["top"] - zone["bottom"]) * 1.5
        if abs(zone_mid - vwap) < tolerance:
            factors.append("vwap")

    # Round number proximity
    zone_mid = (zone["top"] + zone["bottom"]) / 2
    nearest_round = round(zone_mid / 5) * 5
    if abs(zone_mid - nearest_round) < (zone["top"] - zone["bottom"]):
        factors.append("round_number")

    return factors


# ===================================================================
# MACRO SCORING
# ===================================================================

def score_macro(macro_events, earnings_events, direction, hold_hours=8):
    """
    Return a dict describing macro environment for this setup.
    """
    high_impact = [e for e in macro_events if e.get("impact") == "high"]

    # Check if anything scheduled during probable hold period
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

    # Earnings risk
    earnings_risk = len(earnings_events) > 0

    return {
        "high_impact_count": len(high_impact),
        "events_in_hold_period": risk_in_hold,
        "earnings_in_window": earnings_events,
        "hostile": len(risk_in_hold) > 0 or earnings_risk
    }


# ===================================================================
# SETUP CONSTRUCTION
# ===================================================================

def build_setup(symbol, zone, current_price, confluence_factors, volume_info,
                macro_info, daily_bars):
    """
    Build a concrete tradeable setup from a zone.
    Returns None if R:R is insufficient or setup is invalid.
    """
    zone_mid = (zone["top"] + zone["bottom"]) / 2
    zone_height = zone["top"] - zone["bottom"]

    # Scalp: zone mid is within 0.3% of current price (immediately actionable)
    # Swing: zone requires a larger move (standard hold, bigger target)
    distance_pct = abs(current_price - zone_mid) / current_price if current_price else 1
    trade_style = "scalp" if distance_pct <= 0.003 else "swing"
    target_multiplier = 1.5 if trade_style == "scalp" else 2.5

    if zone["type"] == "demand":
        direction = "CALL"
        # Entry: break above zone top after price taps zone
        entry_trigger = f"1m close above {zone['top']:.2f} after tag of zone"
        stop = zone["bottom"] - zone_height * 0.3
        pw = prior_week_levels(daily_bars)
        target = pw["high"] if pw else current_price + (current_price - stop) * target_multiplier
    else:
        direction = "PUT"
        entry_trigger = f"1m close below {zone['bottom']:.2f} after tag of zone"
        stop = zone["top"] + zone_height * 0.3
        pw = prior_week_levels(daily_bars)
        target = pw["low"] if pw else current_price - (stop - current_price) * target_multiplier

    risk = abs(current_price - stop)
    reward = abs(target - current_price)
    rr = reward / risk if risk > 0 else 0

    if rr < 1.5:
        return None  # R:R too poor

    # Determine expiration: weekly for close zones, next-month for further
    # For SPY/QQQ weekly expirations (Mon/Wed/Fri)
    today = datetime.now().date()
    days_to_friday = (4 - today.weekday()) % 7 or 7
    target_expiry = (today + timedelta(days=days_to_friday)).isoformat()

    return {
        "instrument": symbol,
        "direction": direction,
        "zone_top": zone["top"],
        "zone_bottom": zone["bottom"],
        "current_price": current_price,
        "entry_trigger": entry_trigger,
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_points": round(risk, 2),
        "reward_points": round(reward, 2),
        "rr_ratio": round(rr, 2),
        "target_expiry": target_expiry,
        "confluence_factors": confluence_factors,
        "zone_fresh": zone["fresh"],
        "zone_status": zone.get("status", "fresh" if zone.get("fresh") else "tested"),
        "zone_strength": round(zone["strength"], 2),
        "trade_style": trade_style,
        "volume_origin_ratio": volume_info.get("zone_volumes", {}).get(
            id(zone), {}).get("origin_volume_ratio", 0)
    }


# ===================================================================
# CLOSEST MISS SCORING
# ===================================================================

def compute_closest_miss(symbol, zones, candidates, nearest_demand, nearest_supply,
                          current_price, volume_info, daily_bars):
    """
    Compute how close the best available zone was to qualifying as a setup.
    Returns a dict with readiness_pct, passed factors, and failed factors with gaps.
    Returns None if no zones were detected at all.
    """
    ZONE_DETECTED = 15
    ZONE_FRESH    = 20
    RR_PASS       = 25
    ZONE_STRENGTH = 20
    VOLUME_CONF   = 20

    score = 0
    passed = []
    failed = []

    if candidates:
        # A candidate passed R:R — score it directly from its stored data
        best = max(candidates, key=lambda s: s["conviction_score"])

        score += ZONE_DETECTED

        if best["zone_fresh"]:
            score += ZONE_FRESH
            passed.append("Fresh zone")
        else:
            failed.append({"factor": "Zone freshness", "have": "tested",
                           "need": "fresh", "gap": "zone has been revisited"})

        score += RR_PASS
        passed.append(f"R:R {best['rr_ratio']}x ✓")

        if best["zone_strength"] >= 1.5:
            score += ZONE_STRENGTH
            passed.append(f"Zone strength {best['zone_strength']}x")
        else:
            failed.append({"factor": "Zone strength", "have": best["zone_strength"],
                           "need": 1.5, "gap": "candle not explosive enough"})

        vol = best["volume_origin_ratio"]
        if vol >= 1.3:
            score += VOLUME_CONF
            passed.append(f"Volume confirmed ({vol:.1f}x)")
        else:
            failed.append({"factor": "Volume at origin", "have": round(vol, 2),
                           "need": 1.3, "gap": "low volume at zone formation"})

        return {
            "symbol": symbol,
            "zone_top": best["zone_top"],
            "zone_bottom": best["zone_bottom"],
            "direction": best["direction"],
            "readiness_pct": min(score, 100),
            "passed": passed,
            "failed": failed,
        }

    # No candidates — evaluate the nearest fresh zone directly
    best_zone = nearest_demand or nearest_supply
    if not best_zone:
        return None

    direction = "CALL" if best_zone["type"] == "demand" else "PUT"
    score += ZONE_DETECTED

    if best_zone["fresh"]:
        score += ZONE_FRESH
        passed.append("Fresh zone")
    else:
        failed.append({"factor": "Zone freshness", "have": "tested",
                       "need": "fresh", "gap": "zone has been revisited"})

    # Approximate R:R for this zone
    zh = best_zone["top"] - best_zone["bottom"]
    pw = prior_week_levels(daily_bars)
    if direction == "CALL":
        stop = best_zone["bottom"] - zh * 0.3
        target = pw["high"] if pw else current_price + (current_price - stop) * 2.5
    else:
        stop = best_zone["top"] + zh * 0.3
        target = pw["low"] if pw else current_price - (stop - current_price) * 2.5
    risk = abs(current_price - stop)
    reward = abs(target - current_price)
    actual_rr = round(reward / risk, 2) if risk > 0 else 0

    if actual_rr >= 1.5:
        score += RR_PASS
        passed.append(f"R:R {actual_rr}x")
    else:
        failed.append({"factor": "R:R", "have": actual_rr, "need": 1.5,
                       "gap": f"needs {round(max(0, 1.5 - actual_rr), 2)} more"})

    strength = best_zone["strength"]
    if strength >= 1.5:
        score += ZONE_STRENGTH
        passed.append(f"Zone strength {strength:.1f}x")
    else:
        failed.append({"factor": "Zone strength", "have": round(strength, 2),
                       "need": 1.5, "gap": "candle not explosive enough"})

    vol_data = volume_info.get("zone_volumes", {}).get(id(best_zone), {})
    vol_ratio = vol_data.get("origin_volume_ratio", 0)
    if vol_ratio >= 1.3:
        score += VOLUME_CONF
        passed.append(f"Volume confirmed ({vol_ratio:.1f}x)")
    else:
        failed.append({"factor": "Volume at origin", "have": round(vol_ratio, 2),
                       "need": 1.3, "gap": "low volume at zone formation"})

    return {
        "symbol": symbol,
        "zone_top": round(best_zone["top"], 2),
        "zone_bottom": round(best_zone["bottom"], 2),
        "direction": direction,
        "readiness_pct": min(score, 100),
        "passed": passed,
        "failed": failed,
    }


# ===================================================================
# CONVICTION SCORING
# ===================================================================

def score_conviction(setup, macro_info, volume_info):
    """
    Score a setup from 0-10 based on factor convergence.
    Return (score, conviction_label, breakdown).

    NO probability outputs. Just a factor count and a qualitative label.
    """
    score = 0
    breakdown = {}

    # Zone freshness (0-2): fresh = never tested, tested = held on touch, broken = excluded
    zone_status = setup.get("zone_status", "fresh" if setup.get("zone_fresh") else "tested")
    if zone_status == "fresh":
        score += 2
        breakdown["zone_freshness"] = "Fresh (+2)"
    elif zone_status == "tested":
        score += 1
        breakdown["zone_freshness"] = "Tested & holding (+1)"
    else:
        breakdown["zone_freshness"] = "Broken (+0)"

    # Zone strength (0-2)
    if setup["zone_strength"] >= 2.0:
        score += 2
        breakdown["zone_strength"] = f"Explosive origin {setup['zone_strength']}x (+2)"
    elif setup["zone_strength"] >= 1.5:
        score += 1
        breakdown["zone_strength"] = f"Moderate origin {setup['zone_strength']}x (+1)"
    else:
        breakdown["zone_strength"] = f"Weak origin {setup['zone_strength']}x (+0)"

    # Volume at origin (0-2)
    vol_ratio = setup["volume_origin_ratio"]
    if vol_ratio >= 1.8:
        score += 2
        breakdown["volume"] = f"Strong spike at origin ({vol_ratio:.1f}x) (+2)"
    elif vol_ratio >= 1.3:
        score += 1
        breakdown["volume"] = f"Moderate volume ({vol_ratio:.1f}x) (+1)"
    else:
        breakdown["volume"] = f"No volume confirmation ({vol_ratio:.1f}x) (+0)"

    # Confluence factors (0-2)
    conf_count = len(setup["confluence_factors"])
    if conf_count >= 3:
        score += 2
        breakdown["confluence"] = f"{conf_count} factors align (+2)"
    elif conf_count >= 1:
        score += 1
        breakdown["confluence"] = f"{conf_count} factor(s) align (+1)"
    else:
        breakdown["confluence"] = "No additional confluence (+0)"

    # R:R (0-1)
    if setup["rr_ratio"] >= 2.5:
        score += 1
        breakdown["rr"] = f"R:R 1:{setup['rr_ratio']} (+1)"
    else:
        breakdown["rr"] = f"R:R 1:{setup['rr_ratio']} (+0)"

    # Macro penalty (-1 if hostile, 0 if neutral)
    if macro_info["hostile"]:
        score -= 1
        breakdown["macro"] = "Event risk in hold period (−1)"
    else:
        breakdown["macro"] = "No major macro risk in hold period"

    # Label
    if score >= 8:
        label = "High"
    elif score >= 5:
        label = "Medium"
    else:
        label = "Low"

    return score, label, breakdown


# ===================================================================
# TOP-LEVEL PIPELINE
# ===================================================================

def analyze_instrument(symbol, daily_bars, intraday_bars, quote, macro_events, earnings_events):
    """
    Run the full pipeline for one instrument. Returns a dict with
    analysis results and (if viable) a trade setup.
    """
    current_price = quote.get("quote", {}).get("lastPrice") or (
        intraday_bars[-1]["close"] if intraday_bars else 0
    )

    if not current_price or len(intraday_bars) < 20 or len(daily_bars) < 10:
        return {
            "symbol": symbol,
            "error": "Insufficient data",
            "setup": None
        }

    zones = detect_zones(intraday_bars)
    volume_info = detect_volume_spikes(intraday_bars, zones)
    nearest_demand, nearest_supply = find_nearest_zones(zones, current_price)
    trend = detect_trend(daily_bars)
    pw_levels = prior_week_levels(daily_bars)

    # Consider both candidate setups (long from demand, short from supply)
    candidates = []
    for zone in filter(None, [nearest_demand, nearest_supply]):
        confluence = confluence_check(zone, current_price, daily_bars, intraday_bars)
        setup = build_setup(symbol, zone, current_price, confluence,
                            volume_info, None, daily_bars)
        if setup:
            macro_info = score_macro(macro_events, earnings_events, setup["direction"])
            setup["macro"] = macro_info
            score, label, breakdown = score_conviction(setup, macro_info, volume_info)
            setup["conviction_score"] = score
            setup["conviction_label"] = label
            setup["conviction_breakdown"] = breakdown
            candidates.append(setup)

    # Pick best candidate for this symbol
    best = max(candidates, key=lambda s: s["conviction_score"]) if candidates else None

    # Volume ratio: last bar vs session average
    avg_vol = volume_info.get("avg_volume", 0)
    last_vol = intraday_bars[-1].get("volume", 0) if intraday_bars else 0
    volume_ratio = round(last_vol / avg_vol, 2) if avg_vol else None

    near_miss = compute_closest_miss(
        symbol, zones, candidates, nearest_demand, nearest_supply,
        current_price, volume_info, daily_bars
    )

    return {
        "symbol": symbol,
        "current_price": current_price,
        "trend": trend,
        "prior_week_levels": pw_levels,
        "zones_detected": len(zones),
        "demand_zones_count": sum(1 for z in zones if z["type"] == "demand"),
        "supply_zones_count": sum(1 for z in zones if z["type"] == "supply"),
        "volume_ratio": volume_ratio,
        "nearest_demand": nearest_demand,
        "nearest_supply": nearest_supply,
        "recent_volume_spikes": volume_info.get("recent_spikes", []),
        "setup": best,
        "all_candidates": candidates,
        "near_miss": near_miss,
    }


def rank_setups(instrument_analyses):
    """
    Rank instruments by the quality of their best setup.
    The top 1-2 become the day's recommendations.
    """
    ranked = sorted(
        instrument_analyses,
        key=lambda a: (a["setup"]["conviction_score"] if a.get("setup") else -999),
        reverse=True
    )
    return ranked
