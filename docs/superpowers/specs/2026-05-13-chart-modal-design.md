# Chart Modal Design

**Date:** 2026-05-13  
**Status:** Approved

## Overview

Add a click-to-expand chart modal to each setup card in the Pre-Market Research Desk. Clicking a "View Chart" button on a setup card opens a full-screen overlay containing a candlestick chart for that instrument, with supply/demand zone and trade framework levels overlaid.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Placement | Modal overlay per setup card | Keeps dashboard compact; chart is on-demand |
| Timeframe | 30-min (5 days) + daily (1 month) with toggle | Both already fetched on scan; covers intraday context and big-picture |
| Overlays | Zone band, entry trigger, stop, target, current price | Full trade framework visible on chart |
| Library | TradingView Lightweight Charts (CDN) | Purpose-built for financial charts; small (~50kb gzip); first-class price line/band API; matches dark theme with minimal CSS |

## Architecture

### Backend — `/api/chart/<symbol>`

New endpoint that returns the two candle series for a given symbol. The scan endpoint (`/api/scan`) already calls `schwab.get_price_history()` for both series; the chart endpoint makes the same calls independently (on demand, when the modal opens) so the chart data is always fresh and not coupled to when the last scan ran.

**Request:** `GET /api/chart/SPY` or `GET /api/chart/QQQ`

**Response:**
```json
{
  "symbol": "SPY",
  "intraday": [
    { "datetime": 1715000000000, "open": 588.1, "high": 589.4, "low": 587.8, "close": 589.0, "volume": 123456 }
  ],
  "daily": [
    { "datetime": 1714608000000, "open": 582.0, "high": 590.1, "low": 581.5, "close": 589.4, "volume": 5432100 }
  ]
}
```

- `intraday`: 30-minute bars, last 5 days (`period_type=day, period=5, frequency_type=minute, frequency=30`)
- `daily`: daily bars, last 1 month (`period_type=month, period=1, frequency_type=daily, frequency=1`)
- `datetime` values are millisecond Unix timestamps (Lightweight Charts expects seconds; frontend divides by 1000)
- Returns `{"error": "..."}` with HTTP 500 on failure

### Frontend — Trigger

Each setup card's `rec-head` div gains a "View Chart" button, inserted between the summary block and the conviction block:

```html
<button class="chart-btn" onclick="openChartModal(setup)">
  📈 View Chart
</button>
```

Styled to match existing buttons: transparent background, `--border-bright` border, uppercase monospace label. Hover state uses `--accent` (gold).

The `setup` object passed to `openChartModal` is the same JS object already rendered into the card (contains `instrument`, `zone_bottom`, `zone_top`, `entry_trigger`, `stop`, `target`, `current_price`, `direction`).

### Frontend — Modal

A single modal element is added to the page body (reused across all cards):

```html
<div id="chartModal" class="chart-modal-backdrop" style="display:none" onclick="closeChartModal(event)">
  <div class="chart-modal">
    <div class="chart-modal-topbar">
      <!-- symbol, direction badge, current price | timeframe toggle + close button -->
    </div>
    <div id="chartContainer" style="height:340px"></div>
    <div id="chartLegend" class="chart-legend"></div>
  </div>
</div>
```

**Open flow (`openChartModal(setup)`):**
1. Populate topbar with symbol, direction badge, current price, conviction score
2. Show modal (`display: flex`)
3. Fetch `/api/chart/<symbol>`
4. Initialize Lightweight Charts in `#chartContainer` (dark theme matching dashboard palette)
5. Add candlestick series with intraday data (default active timeframe)
6. Draw overlays as price lines:
   - Zone band: two dashed price lines in `--accent` (#c9a961) at `zone_top` and `zone_bottom` — no fill (Lightweight Charts v4 has no native band primitive; an HTML overlay just for fill isn't worth the complexity)
   - Entry trigger: solid blue line (#60a5fa)
   - Target: dashed green line (#4ade80)
   - Stop: dashed red line (#f87171)
   - Current price: dim solid line (#8a95a8)
7. Populate legend strip below chart with all five levels and their prices
8. Scroll to prevent body scroll while modal is open

**Timeframe toggle:**
- Two buttons: `30m` and `1D`
- Switching replaces the candlestick series data without re-fetching (both series stored in memory after initial fetch)
- Active button gets `--bg-card` background + full ink color; inactive is dim

**Close behavior:**
- `✕ Close` button
- Click on backdrop (outside modal box)
- `keydown` listener for `Escape`
- All three call `closeChartModal()` which hides modal, destroys the Lightweight Charts instance (prevents memory leak), and re-enables body scroll

### Library Loading

Lightweight Charts loaded via CDN in `<head>`, before the closing `</head>` tag:

```html
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
```

Version pinned to 4.2.0. No module bundler needed — library exposes `LightweightCharts` as a global.

## Visual Spec

- Modal backdrop: `rgba(5, 7, 10, 0.88)` full-viewport overlay
- Modal box: `background: --bg-card`, `border: 1px solid --border-bright`, max-width 900px, centered
- Chart background: `--bg` (#0a0d12)
- Candlestick up color: `--green` (#4ade80); down color: `--red` (#f87171)
- Legend strip: `background: --bg`, `border: 1px solid --border`, flex row, each item shows a colored swatch + label + price value
- Topbar layout: symbol (Fraunces 28px) + direction badge + current price on left; timeframe toggle + close button on right

## Error Handling

- If `/api/chart/<symbol>` returns an error or the fetch fails, show an inline error message inside `#chartContainer` (same styling as existing `.error-box`) instead of crashing
- If Schwab is not authenticated, the endpoint returns a 500 with an error string; the modal displays it gracefully

## Out of Scope

- Volume bars (not needed for zone/level analysis)
- Drawing tools or zoom controls beyond what Lightweight Charts provides by default
- Persisting the last-viewed timeframe across modal opens
- Charts for instruments other than SPY and QQQ
