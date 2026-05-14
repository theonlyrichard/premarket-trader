# Chart Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a click-to-expand candlestick chart modal to each setup card, showing SPY/QQQ price history with supply/demand zone and trade framework overlays.

**Architecture:** A new Flask endpoint `/api/chart/<symbol>` fetches two candle series (30-min/5-day and daily/1-month) from Schwab on demand. The frontend adds a "View Chart" button to each setup card's header; clicking opens a full-screen overlay that renders a TradingView Lightweight Charts candlestick chart with price-line overlays for the zone, entry, stop, and target levels. A 30m/1D toggle switches datasets without re-fetching.

**Tech Stack:** Python/Flask (backend), TradingView Lightweight Charts v4.2.0 via CDN (frontend), pytest (tests)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `tests/conftest.py` | Create | Flask test client fixture (mocks external deps before import) |
| `tests/test_chart_endpoint.py` | Create | Tests for `/api/chart/<symbol>` |
| `backend/app.py` | Modify | Add `/api/chart/<symbol>` route after existing routes |
| `frontend/index.html` | Modify | CDN script, CSS additions, modal HTML, chart JS, card button |

---

## Task 1: Test scaffold

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Install pytest into the venv**

```
venv\Scripts\pip install pytest
```

Expected output: `Successfully installed pytest-...`

- [ ] **Step 2: Create `tests/conftest.py`**

```python
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Inject mock modules before app.py can import the real ones.
# This prevents SchwabClient, FinnhubClient, etc. from making real API calls
# or needing credentials during tests.
for _mod in ("schwab_client", "finnhub_client", "tracker", "analysis"):
    sys.modules[_mod] = MagicMock()


@pytest.fixture(scope="session")
def flask_app():
    import app as _app
    _app.app.config["TESTING"] = True
    return _app.app


@pytest.fixture
def client(flask_app):
    with flask_app.test_client() as c:
        yield c
```

- [ ] **Step 3: Verify the fixture imports without error**

```
venv\Scripts\pytest tests/ --collect-only
```

Expected: `no tests ran` (or `0 items` — no test files yet, but no import errors).

---

## Task 2: Failing tests for `/api/chart/<symbol>`

**Files:**
- Create: `tests/test_chart_endpoint.py`

- [ ] **Step 1: Create `tests/test_chart_endpoint.py`**

```python
import app as flask_app

FAKE_CANDLES = [
    {"datetime": 1715000000000, "open": 588.1, "high": 589.4,
     "low": 587.8, "close": 589.0, "volume": 123456},
    {"datetime": 1715001800000, "open": 589.0, "high": 590.1,
     "low": 588.5, "close": 589.8, "volume": 98765},
]


def test_chart_returns_intraday_and_daily(client):
    flask_app.schwab.get_price_history.reset_mock()
    flask_app.schwab.get_price_history.return_value = FAKE_CANDLES

    resp = client.get("/api/chart/SPY")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["symbol"] == "SPY"
    assert data["intraday"] == FAKE_CANDLES
    assert data["daily"] == FAKE_CANDLES
    assert flask_app.schwab.get_price_history.call_count == 2


def test_chart_qqq_also_works(client):
    flask_app.schwab.get_price_history.reset_mock()
    flask_app.schwab.get_price_history.return_value = FAKE_CANDLES

    resp = client.get("/api/chart/QQQ")

    assert resp.status_code == 200
    assert resp.get_json()["symbol"] == "QQQ"


def test_chart_rejects_unknown_symbol(client):
    resp = client.get("/api/chart/AAPL")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_chart_handles_schwab_error(client):
    flask_app.schwab.get_price_history.side_effect = RuntimeError("API down")

    resp = client.get("/api/chart/SPY")

    assert resp.status_code == 500
    assert "error" in resp.get_json()

    flask_app.schwab.get_price_history.side_effect = None  # reset for later tests
```

- [ ] **Step 2: Run tests to confirm they all fail**

```
venv\Scripts\pytest tests/test_chart_endpoint.py -v
```

Expected: 4 FAILED with `404 NOT FOUND` (route doesn't exist yet).

---

## Task 3: Backend — `/api/chart/<symbol>` endpoint

**Files:**
- Modify: `backend/app.py` — add route after the `/api/history` route (~line 232)

- [ ] **Step 1: Add the route to `backend/app.py`**

Insert this block immediately after the `history()` function (after its closing `return` statement, before the `health()` route):

```python
@app.route("/api/chart/<symbol>", methods=["GET"])
def chart(symbol):
    """Return intraday (30-min, 5-day) and daily (1-month) candle series."""
    if symbol not in ("SPY", "QQQ"):
        return jsonify({"error": f"unsupported symbol: {symbol}"}), 400
    try:
        intraday = schwab.get_price_history(
            symbol, period_type="day", period=5,
            frequency_type="minute", frequency=30
        )
        daily = schwab.get_price_history(
            symbol, period_type="month", period=1,
            frequency_type="daily", frequency=1
        )
        return jsonify({"symbol": symbol, "intraday": intraday, "daily": daily})
    except Exception as e:
        app.logger.exception("chart fetch failed")
        return jsonify({"error": str(e)}), 500
```

- [ ] **Step 2: Run tests to confirm they all pass**

```
venv\Scripts\pytest tests/test_chart_endpoint.py -v
```

Expected:
```
test_chart_returns_intraday_and_daily PASSED
test_chart_qqq_also_works             PASSED
test_chart_rejects_unknown_symbol     PASSED
test_chart_handles_schwab_error       PASSED
4 passed
```

- [ ] **Step 3: Commit**

```
git add backend/app.py tests/conftest.py tests/test_chart_endpoint.py
git commit -m "feat: add /api/chart/<symbol> endpoint with tests"
```

---

## Task 4: Frontend — CDN, CSS, and modal HTML

**Files:**
- Modify: `frontend/index.html`

All edits are to the single `frontend/index.html` file. Make them in the order listed.

- [ ] **Step 1: Add Lightweight Charts CDN in `<head>`**

Add this line immediately before the closing `</head>` tag (after the closing `</style>` tag):

```html
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
```

- [ ] **Step 2: Update `.rec-head` grid to accommodate the chart button**

In the `<style>` block, find:

```css
  .rec-head {
    padding: 24px 28px 20px;
    display: grid;
    grid-template-columns: auto 1fr auto;
```

Replace `auto 1fr auto` with `auto 1fr auto auto`:

```css
  .rec-head {
    padding: 24px 28px 20px;
    display: grid;
    grid-template-columns: auto 1fr auto auto;
```

- [ ] **Step 3: Add chart-related CSS to the `<style>` block**

Add this block at the end of the `<style>` block, immediately before the closing `</style>` tag:

```css
  /* Chart button on setup cards */
  .chart-btn {
    background: transparent;
    border: 1px solid var(--border-bright);
    color: var(--ink-dim);
    padding: 8px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
    align-self: center;
  }
  .chart-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* Chart modal */
  .chart-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 7, 10, 0.88);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .chart-modal {
    background: var(--bg-card);
    border: 1px solid var(--border-bright);
    width: 100%;
    max-width: 900px;
    padding: 20px 24px 24px;
  }
  .chart-modal-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    flex-wrap: wrap;
    gap: 12px;
  }
  .chart-modal-symbol-block {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .chart-modal-symbol {
    font-family: 'Fraunces', serif;
    font-size: 28px;
    font-weight: 500;
    letter-spacing: -0.02em;
  }
  .chart-modal-meta { font-size: 11px; color: var(--ink-dim); }
  .chart-modal-controls { display: flex; align-items: center; gap: 10px; }
  .timeframe-toggle {
    display: inline-flex;
    border: 1px solid var(--border-bright);
    border-radius: 3px;
    overflow: hidden;
  }
  .tf-btn {
    padding: 5px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--ink-faint);
    transition: all 0.12s;
  }
  .tf-btn.active { background: var(--bg-elevated); color: var(--ink); }
  .chart-container { height: 340px; }
  .chart-legend {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    margin-top: 10px;
    padding: 10px 14px;
    background: var(--bg);
    border: 1px solid var(--border);
    font-size: 11px;
  }
  .chart-legend-item { display: flex; align-items: center; gap: 6px; }
  .chart-legend-swatch { width: 20px; height: 2px; border-radius: 1px; flex-shrink: 0; }
  .chart-legend-name { color: var(--ink-faint); text-transform: uppercase; font-size: 9px; letter-spacing: 0.12em; }
  .chart-legend-val { font-weight: 500; }
  .chart-close-btn {
    background: transparent;
    border: 1px solid var(--border-bright);
    color: var(--ink-dim);
    padding: 5px 10px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .chart-close-btn:hover { border-color: var(--red); color: var(--red); }
```

- [ ] **Step 4: Add modal HTML to the page body**

Add this block immediately before the opening `<script>` tag (near the bottom of `<body>`):

```html
<div id="chartModal" class="chart-modal-backdrop" style="display:none" onclick="closeChartModal(event)">
  <div class="chart-modal" onclick="event.stopPropagation()">
    <div class="chart-modal-topbar">
      <div class="chart-modal-symbol-block">
        <div class="chart-modal-symbol" id="chartModalSymbol"></div>
        <div id="chartModalBadge" class="direction" style="padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.15em"></div>
        <div class="chart-modal-meta" id="chartModalMeta"></div>
      </div>
      <div class="chart-modal-controls">
        <div class="timeframe-toggle">
          <button class="tf-btn active" id="tfBtn30m" onclick="switchChartTimeframe('30m')">30m</button>
          <button class="tf-btn" id="tfBtn1D" onclick="switchChartTimeframe('1D')">1D</button>
        </div>
        <button class="chart-close-btn" onclick="closeChartModal()">✕ Close</button>
      </div>
    </div>
    <div id="chartContainer" class="chart-container"></div>
    <div id="chartLegend" class="chart-legend"></div>
  </div>
</div>
```

- [ ] **Step 5: Verify static structure renders**

Open http://127.0.0.1:5000. The page should look identical to before — modal is `display:none`. No console errors. The CDN script loads (check Network tab: `lightweight-charts.standalone.production.js` → 200).

- [ ] **Step 6: Commit**

```
git add frontend/index.html
git commit -m "feat: add chart modal static structure and CSS"
```

---

## Task 5: Frontend — "View Chart" button on setup cards

**Files:**
- Modify: `frontend/index.html` — two JS changes inside the `<script>` block

- [ ] **Step 1: Add `_currentSetups` module-level variable**

At the top of the `<script>` block, immediately after the existing module-level variable declarations (`let lastScanTime`, `let autoScanEnabled`, etc.), add:

```javascript
let _currentSetups = [];
```

- [ ] **Step 2: Store setups before rendering in `renderRecommendations`**

In `renderRecommendations`, find this line:

```javascript
  const top = viable.slice(0, 4);
  area.innerHTML = `
```

Insert `_currentSetups = top;` between those two lines:

```javascript
  const top = viable.slice(0, 4);
  _currentSetups = top;
  area.innerHTML = `
```

- [ ] **Step 3: Pass index to `renderRecCard` in the map call**

Find this line inside `renderRecommendations`:

```javascript
    ${top.map(renderRecCard).join('')}
```

Replace it with:

```javascript
    ${top.map((s, i) => renderRecCard(s, i)).join('')}
```

- [ ] **Step 4: Add `index` parameter and chart button to `renderRecCard`**

Find the function signature:

```javascript
function renderRecCard(s) {
```

Replace with:

```javascript
function renderRecCard(s, index) {
```

Then inside `renderRecCard`, find the `rec-head` template — specifically the line that opens the conviction block:

```javascript
        <div class="rec-conviction">
```

Insert the chart button immediately before it:

```javascript
        <button class="chart-btn" onclick="openChartModal(${index})">📈 View Chart</button>
        <div class="rec-conviction">
```

- [ ] **Step 5: Add stub `openChartModal` to prevent JS errors**

At the bottom of the `<script>` block, before `</script>`, add:

```javascript
function openChartModal(index) {
  console.log('openChartModal stub — index:', index, _currentSetups[index]);
}
```

- [ ] **Step 6: Verify button appears on setup cards**

Run a scan at http://127.0.0.1:5000. Each setup card should show a "📈 View Chart" button in its header row between the summary and the conviction block. Clicking it logs to the console. No layout breakage.

- [ ] **Step 7: Commit**

```
git add frontend/index.html
git commit -m "feat: add View Chart button to setup cards"
```

---

## Task 6: Frontend — Chart modal JavaScript

**Files:**
- Modify: `frontend/index.html` — replace stub + add full chart JS in `<script>` block

- [ ] **Step 1: Replace the stub `openChartModal` with the full implementation**

Find and delete the stub added in Task 5:

```javascript
function openChartModal(index) {
  console.log('openChartModal stub — index:', index, _currentSetups[index]);
}
```

Replace it with the full implementation:

```javascript
// ── Chart modal ────────────────────────────────────────────────

let _chartData   = null;  // { symbol, intraday: [], daily: [] }
let _chartSetup  = null;  // setup object for the open card
let _chartTf     = '30m'; // active timeframe toggle
let _chartInst   = null;  // LightweightCharts instance
let _candleSeries = null;
let _chartResizeObserver = null;

async function openChartModal(index) {
  const setup = _currentSetups[index];
  if (!setup) return;
  _chartSetup = setup;
  _chartTf = '30m';

  // Populate topbar
  document.getElementById('chartModalSymbol').textContent = setup.instrument;
  const badge = document.getElementById('chartModalBadge');
  badge.className = `direction ${setup.direction}`;
  badge.style.cssText = 'padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.15em';
  badge.textContent = setup.direction;
  document.getElementById('chartModalMeta').textContent =
    `Current ${setup.current_price.toFixed(2)} · Score ${setup.conviction_score}/10`;

  // Show modal
  document.getElementById('chartModal').style.display = 'flex';
  document.body.style.overflow = 'hidden';
  _setActiveTfBtn('30m');

  // Loading placeholder
  const container = document.getElementById('chartContainer');
  container.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--ink-faint);font-size:12px">Loading chart…</div>';
  document.getElementById('chartLegend').innerHTML = '';

  try {
    const resp = await fetch(`/api/chart/${setup.instrument}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    _chartData = data;
    container.innerHTML = '';
    _renderChart('30m');
  } catch (e) {
    container.innerHTML =
      `<div class="error-box" style="margin:0"><strong>Chart error:</strong> ${e.message}</div>`;
  }
}

function closeChartModal(event) {
  // Allow: direct call (no event) or backdrop click (event.target === backdrop)
  if (event && event.target !== document.getElementById('chartModal')) return;
  document.getElementById('chartModal').style.display = 'none';
  document.body.style.overflow = '';
  if (_chartResizeObserver) { _chartResizeObserver.disconnect(); _chartResizeObserver = null; }
  if (_chartInst) { _chartInst.remove(); _chartInst = null; _candleSeries = null; }
  _chartData = null;
  _chartSetup = null;
}

function switchChartTimeframe(tf) {
  if (!_chartData || tf === _chartTf) return;
  _chartTf = tf;
  _setActiveTfBtn(tf);
  if (_chartResizeObserver) { _chartResizeObserver.disconnect(); _chartResizeObserver = null; }
  if (_chartInst) { _chartInst.remove(); _chartInst = null; _candleSeries = null; }
  document.getElementById('chartContainer').innerHTML = '';
  _renderChart(tf);
}

function _setActiveTfBtn(tf) {
  document.getElementById('tfBtn30m').className = tf === '30m' ? 'tf-btn active' : 'tf-btn';
  document.getElementById('tfBtn1D').className  = tf === '1D'  ? 'tf-btn active' : 'tf-btn';
}

function _renderChart(tf) {
  const container = document.getElementById('chartContainer');
  const setup = _chartSetup;
  const candles = tf === '30m' ? _chartData.intraday : _chartData.daily;

  _chartInst = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 340,
    layout: { background: { color: '#0a0d12' }, textColor: '#8a95a8' },
    grid: { vertLines: { color: '#1a2030' }, horzLines: { color: '#1a2030' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#242b38' },
    timeScale: { borderColor: '#242b38', timeVisible: true },
  });

  _candleSeries = _chartInst.addCandlestickSeries({
    upColor: '#4ade80', downColor: '#f87171',
    borderUpColor: '#4ade80', borderDownColor: '#f87171',
    wickUpColor: '#4ade80', wickDownColor: '#f87171',
  });

  // Schwab returns datetime in milliseconds; Lightweight Charts expects seconds
  _candleSeries.setData(
    candles
      .map(c => ({ time: Math.floor(c.datetime / 1000), open: c.open, high: c.high, low: c.low, close: c.close }))
      .sort((a, b) => a.time - b.time)
  );

  // Entry price: zone_top for CALL (break above), zone_bottom for PUT (break below)
  const entryPrice = setup.direction === 'CALL' ? setup.zone_top : setup.zone_bottom;

  const overlays = [
    { price: setup.target,    color: '#4ade80', style: LightweightCharts.LineStyle.Dashed, title: 'Target'  },
    { price: entryPrice,      color: '#60a5fa', style: LightweightCharts.LineStyle.Solid,  title: 'Entry'   },
    { price: setup.zone_top,  color: '#c9a961', style: LightweightCharts.LineStyle.Dashed, title: 'Zone ▲'  },
    { price: setup.zone_bottom, color: '#c9a961', style: LightweightCharts.LineStyle.Dashed, title: 'Zone ▼' },
    { price: setup.stop,      color: '#f87171', style: LightweightCharts.LineStyle.Dashed, title: 'Stop'    },
    { price: setup.current_price, color: '#5a6578', style: LightweightCharts.LineStyle.Solid, title: 'Price' },
  ];
  overlays.forEach(({ price, color, style, title }) => {
    _candleSeries.createPriceLine({ price, color, lineStyle: style, lineWidth: 1, axisLabelVisible: true, title });
  });

  _chartInst.timeScale().fitContent();

  // Resize handler so chart fills container if window is resized
  _chartResizeObserver = new ResizeObserver(() => {
    if (_chartInst) _chartInst.applyOptions({ width: container.clientWidth });
  });
  _chartResizeObserver.observe(container);

  _renderChartLegend(setup, entryPrice);
}

function _renderChartLegend(setup, entryPrice) {
  const items = [
    { label: 'Target',  val: setup.target.toFixed(2),           color: '#4ade80' },
    { label: 'Entry',   val: entryPrice.toFixed(2),              color: '#60a5fa' },
    { label: 'Zone',    val: `${setup.zone_bottom.toFixed(2)} – ${setup.zone_top.toFixed(2)}`, color: '#c9a961' },
    { label: 'Stop',    val: setup.stop.toFixed(2),              color: '#f87171' },
    { label: 'Current', val: setup.current_price.toFixed(2),    color: '#8a95a8' },
  ];
  document.getElementById('chartLegend').innerHTML = items.map(({ label, val, color }) => `
    <div class="chart-legend-item">
      <div class="chart-legend-swatch" style="background:${color}"></div>
      <span class="chart-legend-name">${label}</span>
      <span class="chart-legend-val" style="color:${color}">${val}</span>
    </div>
  `).join('');
}

// Close modal on ESC key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('chartModal').style.display !== 'none') {
    closeChartModal();
  }
});
```

- [ ] **Step 2: Verify the modal opens and renders**

1. Open http://127.0.0.1:5000 and run a scan.
2. Click "📈 View Chart" on any setup card.
3. Confirm: modal opens, loading text shows briefly, candlestick chart renders with colored lines for target (green), entry (blue), zone (gold ×2), stop (red), current price (dim).
4. Confirm: legend strip below chart shows all five levels with correct colors and prices.
5. Click "30m" / "1D" toggle — chart re-renders with the other dataset.
6. Press ESC — modal closes. Scroll works again on the main page.
7. Click the "✕ Close" button on another open — modal closes.
8. Click the backdrop (outside modal box) — modal closes.

- [ ] **Step 3: Commit**

```
git add frontend/index.html
git commit -m "feat: implement chart modal with Lightweight Charts and level overlays"
```

---

## Done

All four files have been modified/created. The feature is complete when:
- `venv\Scripts\pytest tests/ -v` shows 4 passing tests
- The chart modal opens from any setup card, shows candles with overlays, toggles timeframes, and closes cleanly via button / backdrop / ESC
