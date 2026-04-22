"""
Finnhub API client.

Free tier: 60 calls/minute, unlimited per day. For our daily use case
(one scan per morning) this is vastly more than enough.

Docs: https://finnhub.io/docs/api
"""

import requests
from datetime import date


class FinnhubClient:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key):
        self.api_key = api_key

    def _get(self, endpoint, params=None):
        params = params or {}
        params["token"] = self.api_key
        resp = requests.get(f"{self.BASE_URL}{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def economic_calendar(self, from_date, to_date):
        """
        Returns US economic events (Fed, CPI, NFP, GDP, PMI, etc).
        Each event has: country, event, time, impact (low/medium/high),
        actual, estimate, prev.
        """
        data = self._get("/calendar/economic", {
            "from": str(from_date),
            "to": str(to_date)
        })
        events = data.get("economicCalendar", [])
        # Filter to US-only by default since we're trading US indices
        return [e for e in events if e.get("country") == "US"]

    def earnings_calendar(self, from_date, to_date, tickers=None):
        """
        Returns earnings announcements.
        If tickers provided, filter to just those symbols.
        """
        data = self._get("/calendar/earnings", {
            "from": str(from_date),
            "to": str(to_date)
        })
        earnings = data.get("earningsCalendar", [])
        if tickers:
            earnings = [e for e in earnings if e.get("symbol") in tickers]
        return earnings

    def market_news(self, category="general", min_id=0):
        """Latest market news, useful for surfacing overnight headlines."""
        return self._get("/news", {"category": category, "minId": min_id})
