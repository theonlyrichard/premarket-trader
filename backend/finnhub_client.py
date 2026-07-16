"""
Finnhub API client.

Free tier: 60 calls/minute. Economic calendar is often paywalled (403) on
free keys — this client degrades gracefully so the desk still runs.

Docs: https://finnhub.io/docs/api
"""

import logging
import requests
from datetime import date

log = logging.getLogger(__name__)


class FinnhubClient:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()

    def _get(self, endpoint, params=None, optional=False):
        params = params or {}
        params["token"] = self.api_key
        try:
            resp = requests.get(
                f"{self.BASE_URL}{endpoint}", params=params, timeout=10
            )
            if resp.status_code in (401, 403):
                msg = f"Finnhub {endpoint} → {resp.status_code} (plan/key blocked)"
                log.warning(msg)
                if optional:
                    return None
                # Still optional for desk resilience
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning("Finnhub %s failed: %s", endpoint, e)
            if optional:
                return None
            return None

    def economic_calendar(self, from_date, to_date):
        """
        US economic events. Free tier often returns 403 — returns [] then.
        """
        data = self._get(
            "/calendar/economic",
            {"from": str(from_date), "to": str(to_date)},
            optional=True,
        )
        if not data:
            return []
        events = data.get("economicCalendar", [])
        return [e for e in events if e.get("country") == "US"]

    def earnings_calendar(self, from_date, to_date, tickers=None):
        data = self._get(
            "/calendar/earnings",
            {"from": str(from_date), "to": str(to_date)},
            optional=True,
        )
        if not data:
            return []
        earnings = data.get("earningsCalendar", [])
        if tickers:
            earnings = [e for e in earnings if e.get("symbol") in tickers]
        return earnings

    def market_news(self, category="general", min_id=0):
        data = self._get(
            "/news", {"category": category, "minId": min_id}, optional=True
        )
        return data or []
