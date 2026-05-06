# Strike & Expiry Tier Recommendations — Design Spec

**Date:** 2026-05-06  
**Status:** Approved  

---

## Overview

Replace the current single-contract option suggestion on each setup card with a **3-tier pill row** (Aggressive / Moderate / Conservative). Each tier targets a different delta and expiry window appropriate to the setup's trade style (scalp vs swing) and conviction level. One tier is dynamically starred (★) as the recommended pick.

---

## Backend — `app.py`

### Option Chain Fetch

Replace the current two-step fetch (exact target date → fallback to +7 days) with a single wider fetch: **today → today + 21 days**. One request per setup, covers all three tier windows.

```python
from_str = today.isoformat()
to_str = (today + timedelta(days=21)).isoformat()
chain = schwab.get_option_chain(
    symbol=setup["instrument"],
    contract_type=setup["direction"],
    from_date=from_str,
    to_date=to_str
)
setup["strike_tiers"] = pick_strike_tiers(chain, setup, today)
```

`strike_recommendation` is removed from the setup dict. `strike_tiers` replaces it.

---

### `pick_strike_tiers(chain, setup, today)` — New Function

Selects the best contract per tier from the fetched chain. Returns a list of up to 3 tier objects.

#### DTE Windows by Tier and Trade Style

| Tier | Scalp DTE range | Swing DTE range | Target delta |
|---|---|---|---|
| `aggressive` | 0–1 DTE | 3–7 DTE | ~0.60 |
| `moderate` | 2–5 DTE | 7–14 DTE | ~0.45 |
| `conservative` | 6–14 DTE | 14–21 DTE | ~0.35 |

Best contract per tier = contract in window with delta closest to target. PUT deltas are negative from the API — use `abs(delta)` for comparison.

If a window has no contracts in the chain, that tier is omitted (pills render whatever tiers are available).

#### Dynamic ★ Recommended Tier

| Condition | Recommended tier |
|---|---|
| Macro hostile OR conviction == "Low" | `conservative` |
| Scalp + conviction == "High" | `aggressive` |
| Everything else | `moderate` |

#### Tier Object Schema

```json
{
  "tier": "moderate",
  "recommended": true,
  "symbol": "SPY 524C",
  "strike": 524.0,
  "expiration": "2026-05-09",
  "dte": 3,
  "delta": 0.45,
  "theta": -0.08,
  "iv": 0.16,
  "bid": 2.20,
  "ask": 2.60,
  "mid": 2.40
}
```

`mid` is `(bid + ask) / 2`, rounded to 2 decimal places. If bid or ask is `None`, mid is `None`.

---

## Frontend — `index.html`

### Replace `option-strike` Block in `renderRecCard()`

The existing single-contract block is replaced with a **3-pill row**. Each pill shows:

- Tier label (color-coded: red=Aggressive, amber=Moderate, green=Conservative)
- Strike + direction abbreviation (e.g. `524C`)
- Expiry date + DTE (e.g. `May 9 · 3dte`)
- Δ and IV
- Mid-price cost estimate (e.g. `~$2.40`)

The ★ recommended pill gets a subtle accent-colored border to distinguish it.

### Fallback

If `strike_tiers` is absent or empty (Schwab not authenticated or no contracts in range), show the existing "Fetch option chain to populate strike (requires Schwab auth)" message unchanged.

### CSS Additions

~40 lines added to the existing `<style>` block:
- `.tier-row` — 3-column grid container
- `.tier-pill` — individual pill base styles
- `.tier-pill.aggressive`, `.tier-pill.moderate`, `.tier-pill.conservative` — color themes
- `.tier-pill.recommended` — accent border highlight

No new files. No external dependencies.

---

## Scope Boundaries

### In Scope

- `pick_strike_tiers()` replaces `pick_strike()` in `app.py`
- Option chain fetch widens to +21 days (single fetch, no fallback)
- Frontend pill row replaces `option-strike` block
- `strike_tiers` added to API response; `strike_recommendation` removed

### Out of Scope

- No changes to `analysis.py`, `schwab_client.py`, `tracker.py`, `finnhub_client.py`
- No changes to history table, tracker stats, or macro banner
- No new API endpoints
- History table does not track which tier the user chose

### Edge Cases

- **No contracts in a DTE window:** tier is omitted; 1 or 2 pills render instead of 3
- **PUT delta sign:** always use `abs(delta)` for target comparison
- **Missing bid/ask:** mid-price field renders `—`
- **Schwab not authenticated:** existing fallback message shown, no pills rendered
