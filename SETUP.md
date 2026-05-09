# Pre-Market Research Desk — Setup Guide

## Quick Start (after first-time setup is done)

Open a terminal and run these commands:

```
cd C:\Users\richa\Desktop\VibeCodedProjects\premarket-trader
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd backend
python app.py
```

> `pip install` only needed the first time. Skip it on subsequent runs.

Then open **http://127.0.0.1:5000** in your browser and click **Run Morning Scan**.

To stop the server: `Ctrl+C`

---

This is a local dashboard that runs on your computer. It pulls pre-market data from Schwab and macro events from Finnhub, analyzes SPY and QQQ for supply/demand setups with volume confirmation, and gives you up to 2 trade recommendations each morning.

The whole stack is free. No subscriptions. You need Python installed and a Schwab brokerage account.

Estimated one-time setup: 30-45 minutes, mostly waiting on Schwab's app approval.

---

## What you need before starting

1. **Python 3.9 or newer** installed. Test with `python --version` in a terminal.
2. **Schwab brokerage account** (free). Your existing trading account works.
3. **A web browser** for OAuth login.

---

## Step 1 — Register for the Schwab Developer Portal

This is the slowest part. Schwab reviews app registrations manually and it usually takes a few business days.

1. Go to **https://developer.schwab.com** and create a developer account. Use a different email than your brokerage login if you want to keep them separate, but linking is fine.
2. Verify your email.
3. On the welcome page, choose **Individual Developer** role. This is the retail trader path. Don't pick Fintech or Advisor.
4. Once you're in, click **Create App**. Fill in:
   - **App name**: anything (e.g., "Personal Trading Research")
   - **Description**: keep it simple — "Personal trading automation for market data analysis" works. Don't oversell it, apps described as "institutional" or "high-frequency" get rejected.
   - **App type**: Individual
   - **Callback URL**: `https://127.0.0.1` (exactly this, no trailing slash, no port)
   - **API products**: select **Accounts and Trading Production** AND **Market Data Production**
5. Submit. You'll see the app status as **Approved - Pending** or similar. Wait for the status to become **Ready for Use**. This typically takes 1-3 business days. They'll email you.
6. Once approved, open your app and copy the **App Key** and **App Secret**. Keep these safe — they're like passwords.

---

## Step 2 — Get a Finnhub API key

1. Go to **https://finnhub.io** and sign up (free, no credit card).
2. Your API key is on your dashboard immediately. Copy it.
3. Free tier is 60 calls/minute, which is way more than this tool uses.

---

## Step 3 — Install the app

Open a terminal. Navigate to where you extracted the `premarket-trader` folder.

```
cd premarket-trader
```

Create a virtual environment (keeps dependencies isolated):

```
python -m venv venv
```

Activate it. On macOS/Linux:

```
source venv/bin/activate
```

On Windows:

```
venv\Scripts\activate
```

Install dependencies:

```
pip install -r requirements.txt
```

---

## Step 4 — Configure your API keys

Copy the example config file:

```
cp config.example.json config.json
```

(On Windows: `copy config.example.json config.json`)

Open `config.json` in any text editor and paste in your keys:

```json
{
  "schwab": {
    "app_key": "paste_schwab_app_key_here",
    "app_secret": "paste_schwab_app_secret_here",
    "callback_url": "https://127.0.0.1"
  },
  "finnhub": {
    "api_key": "paste_finnhub_key_here"
  }
}
```

Save. This file is in `.gitignore` so it won't accidentally get committed if you push to GitHub.

---

## Step 5 — Complete Schwab OAuth (one-time)

Run the OAuth setup script:

```
cd backend
python setup_auth.py
```

It will print a URL. Copy it and paste into your browser. You'll:

1. Log in with your **Schwab brokerage** credentials (not your developer portal credentials).
2. Select which account(s) the app can access.
3. Click Allow.
4. Your browser will redirect to `https://127.0.0.1/?code=abc123...` and show an error page saying "can't reach this site". **That's expected.** The code you need is in the URL bar.
5. Copy the **entire URL** from the address bar and paste it back into the terminal prompt.

You should see "Success! Tokens saved." After this, the tool auto-refreshes tokens and you don't need to do this again, usually for at least 7 days.

---

## Step 6 — Run the dashboard

From the `backend` folder:

```
python app.py
```

You'll see:

```
Pre-Market Research Desk
Schwab authenticated: True
Open http://127.0.0.1:5000 in your browser.
```

Open that URL. Click **Run Morning Scan**. The dashboard will:

- Pull today's economic calendar and major earnings
- Pull 5 days of intraday bars and 20 days of daily bars for SPY and QQQ
- Detect supply/demand zones with volume confirmation
- Check technical confluence (EMAs, prior week levels, VWAP, round numbers)
- Build tradeable setups with entry, stop, target, R:R
- Rank them by factor score and show the top 1-2
- Pull the closest-delta options contract for each recommended direction

---

## Daily workflow

Once setup is done, your morning routine is:

1. Start the app: `python app.py` (from `backend/` with venv active).
2. Open http://127.0.0.1:5000.
3. Click **Run Morning Scan**.
4. Review the recommendation(s).
5. Either take the trade manually in your broker, or skip it.

You can leave the app running all day or stop it with Ctrl-C when done.

---

## Understanding the outputs

### Conviction labels

- **High** (score 8-10): All major factors aligned. Fresh zone, strong origin move, volume confirmation, confluence, clean macro.
- **Medium** (score 5-7): Solid setup with some missing factors. Defensible trade but not exceptional.
- **Low** (score below 5): Technically a valid setup but weak. Usually not worth taking.

These are **not probabilities**. Score 8 does not mean "80% win rate". These are factor counts. The actual win rate is measured empirically by the silent outcome tracker.

### Outcome tracker

The bar at the top of the dashboard shows your actual realized win rate over the last 30 days, broken down by conviction label. This is the number that tells you whether the methodology has edge in practice.

You need roughly 20-30 closed trades before the win rate means anything. Before that it'll say "Collecting data…".

### "No trade today"

If the algorithm can't find a setup meeting minimum criteria (R:R at least 1.5, detectable zone, etc.), it shows no-trade. This is a feature, not a bug. Most days are no-trade days. You said you wanted 1-2 trades a week, so this is expected.

---

## Troubleshooting

**"Schwab authenticated: False" on startup**
Run `python setup_auth.py` again. Your refresh token may have expired (7+ days since last auth).

**"401 Unauthorized" errors during scan**
Same as above. Refresh token expired.

**"Connection refused" when opening localhost:5000**
The app isn't running. Open another terminal, activate the venv, and run `python app.py`.

**Scan returns "Insufficient data"**
You're probably running it on a weekend or holiday when there's no fresh data. Try on a trading day pre-market.

**Want to reset outcome tracking**
Delete `data/tracker.db`. Don't do this unless you're sure, you'll lose your win-rate history.

---

## Security notes

- Your Schwab credentials are stored only in `data/schwab_tokens.json`, never transmitted anywhere except to Schwab's own API.
- The app binds to `127.0.0.1` only, which means only programs on your own computer can talk to it. It's not exposed to the internet.
- `config.json` and `data/` are in `.gitignore` to prevent accidental secret leaks.
- If you ever think your keys are compromised, go to the Schwab developer portal and regenerate them, then re-run `setup_auth.py`.

---

## What's next

After 2-4 weeks of use, check the tracker stats. If High-conviction setups are winning above 60% with R:R averaging 1:2+, you have an edge. If they're winning 40-50%, the methodology needs tuning, and we can look at what factors correlate with your actual winners vs. losers.

That data-driven tuning is Phase 3 of the roadmap. Come back and I'll help you analyze the tracker.db once you have enough history.
