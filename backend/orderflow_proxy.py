"""
Orderflow PROXY from OHLC+volume bars (Schwab free data).

This is NOT true orderflow (no footprint, bid/ask delta, or tape).
It is a deterministic approximation for Carmine-style confirmation:

  - relative volume (RVOL)
  - crude volume profile (POC / VAH / VAL)
  - bullish/bearish bar volume imbalance proxy
  - absorption candidates = high vol + small range (don't chase labels)

Never present as precise tape truth. UI must mark as "proxy".
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional


def _safe_mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else 0.0


def relative_volume(bars: List[dict], lookback: int = 40, recent: int = 6) -> dict:
    if not bars or len(bars) < 10:
        return {"rvol": None, "avg_vol": 0, "recent_vol": 0, "label": "n/a"}

    use = bars[-lookback:]
    vols = [float(b.get("volume") or 0) for b in use]
    avg = _safe_mean(vols[:-recent] if len(vols) > recent else vols) or 1.0
    recent_vol = _safe_mean(vols[-recent:])
    rvol = recent_vol / avg if avg else None
    if rvol is None:
        label = "n/a"
    elif rvol >= 1.8:
        label = "hot"
    elif rvol >= 1.25:
        label = "elevated"
    elif rvol <= 0.7:
        label = "thin"
    else:
        label = "normal"
    return {
        "rvol": round(rvol, 2) if rvol is not None else None,
        "avg_vol": round(avg, 1),
        "recent_vol": round(recent_vol, 1),
        "label": label,
    }


def bar_volume_imbalance(bars: List[dict], lookback: int = 30) -> dict:
    """
    Proxy imbalance: allocate each bar's volume by candle body fraction to buy/sell.
    Close > open → buy-proxy volume; else sell-proxy. Doji split 50/50.
    """
    if not bars:
        return {"buy_proxy": 0, "sell_proxy": 0, "imbalance": 0, "bias": "flat"}

    use = bars[-lookback:]
    buy = sell = 0.0
    for b in use:
        vol = float(b.get("volume") or 0)
        o, c = float(b["open"]), float(b["close"])
        h, l = float(b["high"]), float(b["low"])
        rng = max(h - l, 1e-9)
        body = abs(c - o)
        body_frac = min(1.0, body / rng)
        if c > o:
            buy += vol * (0.5 + 0.5 * body_frac)
            sell += vol * (0.5 - 0.5 * body_frac)
        elif c < o:
            sell += vol * (0.5 + 0.5 * body_frac)
            buy += vol * (0.5 - 0.5 * body_frac)
        else:
            buy += vol * 0.5
            sell += vol * 0.5

    total = buy + sell
    imb = (buy - sell) / total if total else 0.0
    if imb > 0.12:
        bias = "buyers"
    elif imb < -0.12:
        bias = "sellers"
    else:
        bias = "balanced"
    return {
        "buy_proxy": round(buy, 1),
        "sell_proxy": round(sell, 1),
        "imbalance": round(imb, 3),
        "bias": bias,
    }


def volume_profile(bars: List[dict], bins: int = 24) -> dict:
    """
    Approximate volume-at-price from bar ranges: distribute each bar's volume
    uniformly across bins it spans. Returns POC, VAH, VAL (~70% value area).
    """
    if not bars or len(bars) < 5:
        return {"poc": None, "vah": None, "val": None, "bins": [], "profile_note": "insufficient"}

    lows = [float(b["low"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        return {"poc": lo, "vah": hi, "val": lo, "bins": [], "profile_note": "flat"}

    width = (hi - lo) / bins
    hist = [0.0] * bins
    for b in bars:
        vol = float(b.get("volume") or 0)
        bl, bh = float(b["low"]), float(b["high"])
        # bins touched
        i0 = max(0, min(bins - 1, int((bl - lo) / width)))
        i1 = max(0, min(bins - 1, int((bh - lo) / width)))
        if i1 < i0:
            i0, i1 = i1, i0
        n = i1 - i0 + 1
        share = vol / n if n else vol
        for i in range(i0, i1 + 1):
            hist[i] += share

    total = sum(hist) or 1.0
    poc_i = max(range(bins), key=lambda i: hist[i])
    poc = lo + (poc_i + 0.5) * width

    # Value area ~70% surrounding POC
    target = total * 0.70
    covered = hist[poc_i]
    left = right = poc_i
    while covered < target and (left > 0 or right < bins - 1):
        left_v = hist[left - 1] if left > 0 else -1
        right_v = hist[right + 1] if right < bins - 1 else -1
        if right_v >= left_v and right < bins - 1:
            right += 1
            covered += hist[right]
        elif left > 0:
            left -= 1
            covered += hist[left]
        else:
            break

    val = lo + left * width
    vah = lo + (right + 1) * width
    levels = [
        {
            "price_mid": round(lo + (i + 0.5) * width, 2),
            "price_lo": round(lo + i * width, 2),
            "price_hi": round(lo + (i + 1) * width, 2),
            "volume": round(hist[i], 1),
            "pct": round(100 * hist[i] / total, 1),
            "is_poc": i == poc_i,
        }
        for i in range(bins)
    ]
    # top 5 HVN
    top = sorted(levels, key=lambda x: x["volume"], reverse=True)[:5]
    return {
        "poc": round(poc, 2),
        "vah": round(vah, 2),
        "val": round(val, 2),
        "range_low": round(lo, 2),
        "range_high": round(hi, 2),
        "bins": levels,
        "high_volume_nodes": top,
        "profile_note": "proxy from OHLC ranges — not tick VPVR",
    }


def absorption_candidates(bars: List[dict], lookback: int = 40, top_n: int = 5) -> List[dict]:
    """
    High volume + compressed range ≈ possible absorption / defending inventory.
    Heuristic only.
    """
    if not bars or len(bars) < 15:
        return []
    use = bars[-lookback:]
    ranges = [float(b["high"]) - float(b["low"]) for b in use]
    vols = [float(b.get("volume") or 0) for b in use]
    avg_r = _safe_mean(ranges) or 1e-9
    avg_v = _safe_mean(vols) or 1.0

    cands = []
    for b in use:
        rng = float(b["high"]) - float(b["low"])
        vol = float(b.get("volume") or 0)
        if vol < avg_v * 1.5:
            continue
        if rng > avg_r * 0.85:
            continue  # want tight range vs average
        score = (vol / avg_v) * (avg_r / max(rng, 1e-9))
        side = "bid_defenders" if float(b["close"]) >= float(b["open"]) else "offer_defenders"
        cands.append({
            "price": round((float(b["high"]) + float(b["low"])) / 2, 2),
            "low": round(float(b["low"]), 2),
            "high": round(float(b["high"]), 2),
            "volume": round(vol, 1),
            "range": round(rng, 3),
            "vol_x": round(vol / avg_v, 2),
            "score": round(score, 2),
            "side_proxy": side,
            "time": b.get("datetime"),
            "session": None,  # filled by caller if desired
        })
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands[:top_n]


def zone_volume_context(zones: List[dict], profile: dict, price: float) -> List[dict]:
    """Relate nearest zones to POC/VAH/VAL."""
    if not zones or not profile.get("poc"):
        return []
    out = []
    poc, vah, val = profile.get("poc"), profile.get("vah"), profile.get("val")
    for z in zones[:8]:
        mid = (float(z.get("top", 0)) + float(z.get("bottom", 0))) / 2
        flags = []
        tol = max(abs(float(z.get("top", 0)) - float(z.get("bottom", 0))), (price or 1) * 0.001)
        if poc is not None and abs(mid - poc) <= tol * 1.5:
            flags.append("at_poc")
        if vah is not None and abs(mid - vah) <= tol * 1.5:
            flags.append("at_vah")
        if val is not None and abs(mid - val) <= tol * 1.5:
            flags.append("at_val")
        if vah is not None and val is not None and val <= mid <= vah:
            flags.append("inside_value")
        out.append({
            "type": z.get("type"),
            "pattern": z.get("pattern"),
            "timeframe": z.get("timeframe"),
            "top": z.get("top"),
            "bottom": z.get("bottom"),
            "volume_flags": flags,
            "mid": round(mid, 2),
        })
    return out


def build_orderflow_proxy(
    bars: List[dict],
    zones: Optional[List[dict]] = None,
    current_price: Optional[float] = None,
    timeframe: str = "5m",
) -> Dict[str, Any]:
    """
    Full proxy pack for one symbol.
    Prefer finer bars (5m) for absorption / imbalance; longer window ok.
    """
    if not bars:
        return {
            "available": False,
            "disclaimer": "No bars for orderflow proxy.",
            "timeframe": timeframe,
        }

    rvol = relative_volume(bars)
    imb = bar_volume_imbalance(bars)
    prof = volume_profile(bars, bins=24)
    absorb = absorption_candidates(bars)
    zctx = zone_volume_context(zones or [], prof, current_price or 0)

    # Confirmation suggestion vs nearest demand/supply from zones list
    confirm = "neutral"
    if zones:
        dem = [z for z in zones if z.get("type") == "demand" and not z.get("mitigated")]
        sup = [z for z in zones if z.get("type") == "supply" and not z.get("mitigated")]
        if imb["bias"] == "buyers" and rvol.get("label") in ("elevated", "hot") and dem:
            confirm = "supports_long_at_demand"
        elif imb["bias"] == "sellers" and rvol.get("label") in ("elevated", "hot") and sup:
            confirm = "supports_short_at_supply"
        elif rvol.get("label") == "thin":
            confirm = "thin_liquidity_caution"

    narrative_bits = [
        f"RVOL {rvol.get('rvol')} ({rvol.get('label')})",
        f"imbalance proxy → {imb.get('bias')} ({imb.get('imbalance')})",
    ]
    if prof.get("poc") is not None:
        narrative_bits.append(
            f"profile POC {prof['poc']} · VA {prof.get('val')}–{prof.get('vah')}"
        )
    if absorb:
        narrative_bits.append(
            f"{len(absorb)} absorption candidate(s) top {absorb[0]['price']}"
        )
    narrative_bits.append(f"read as {confirm.replace('_', ' ')}")

    return {
        "available": True,
        "timeframe": timeframe,
        "disclaimer": (
            "ORDERFLOW PROXY only — derived from OHLC+volume, not footprint/delta/CVD. "
            "Do not treat as Bookmap/Jigsaw truth."
        ),
        "relative_volume": rvol,
        "imbalance_proxy": imb,
        "volume_profile": {
            "poc": prof.get("poc"),
            "vah": prof.get("vah"),
            "val": prof.get("val"),
            "range_low": prof.get("range_low"),
            "range_high": prof.get("range_high"),
            "high_volume_nodes": prof.get("high_volume_nodes", []),
            "profile_note": prof.get("profile_note"),
        },
        "absorption_candidates": absorb,
        "zone_volume_context": zctx,
        "confirmation": confirm,
        "narrative": " · ".join(narrative_bits),
    }
