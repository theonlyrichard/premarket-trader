"""
Schwab API client.

Handles OAuth 2.0 authentication (including token refresh) and wraps the
endpoints we need: price history, quotes, and options chains.

Schwab docs: https://developer.schwab.com/products/trader-api--individual
"""

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests


class SchwabClient:
    BASE_URL = "https://api.schwabapi.com"
    AUTH_URL = f"{BASE_URL}/v1/oauth/authorize"
    TOKEN_URL = f"{BASE_URL}/v1/oauth/token"
    MARKET_DATA_URL = f"{BASE_URL}/marketdata/v1"

    def __init__(self, app_key, app_secret, callback_url, token_path):
        self.app_key = app_key
        self.app_secret = app_secret
        self.callback_url = callback_url
        self.token_path = Path(token_path)
        self.tokens = self._load_tokens()

    # -----------------------------------------------------------
    # Token management
    # -----------------------------------------------------------
    def _load_tokens(self):
        if self.token_path.exists():
            with open(self.token_path) as f:
                return json.load(f)
        return {}

    def _save_tokens(self, tokens):
        tokens["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.token_path, "w") as f:
            json.dump(tokens, f, indent=2)
        self.tokens = tokens

    def is_authenticated(self):
        return bool(self.tokens.get("refresh_token"))

    def get_auth_url(self):
        params = {
            "client_id": self.app_key,
            "redirect_uri": self.callback_url,
            "response_type": "code",
            "scope": "readonly"
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, callback_url_with_code):
        """Exchange the authorization code in the callback URL for tokens."""
        # Extract code from URL
        if "code=" not in callback_url_with_code:
            raise ValueError("No 'code' parameter in callback URL")
        code = callback_url_with_code.split("code=")[1].split("&")[0]
        code = requests.utils.unquote(code)

        credentials = f"{self.app_key}:{self.app_secret}"
        auth_header = base64.b64encode(credentials.encode()).decode()

        resp = requests.post(
            self.TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.callback_url
            },
            timeout=15
        )
        resp.raise_for_status()
        self._save_tokens(resp.json())

    def _refresh_access_token(self):
        if not self.tokens.get("refresh_token"):
            raise RuntimeError("No refresh token. Complete OAuth flow first.")

        credentials = f"{self.app_key}:{self.app_secret}"
        auth_header = base64.b64encode(credentials.encode()).decode()

        resp = requests.post(
            self.TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"]
            },
            timeout=15
        )
        resp.raise_for_status()
        # Schwab keeps the same refresh token in most refresh cycles, but merge to be safe
        new_tokens = resp.json()
        merged = {**self.tokens, **new_tokens}
        self._save_tokens(merged)

    def _access_token(self):
        """Get a valid access token, refreshing if necessary."""
        saved_at = self.tokens.get("saved_at")
        expires_in = self.tokens.get("expires_in", 1800)
        if saved_at:
            saved_dt = datetime.fromisoformat(saved_at)
            if saved_dt.tzinfo is None:
                saved_dt = saved_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - saved_dt).total_seconds()
            if age >= expires_in - 60:  # refresh 60s before expiry
                self._refresh_access_token()
        elif self.tokens.get("access_token"):
            # No timestamp, play it safe and refresh
            self._refresh_access_token()
        return self.tokens["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token()}"}

    # -----------------------------------------------------------
    # Market data endpoints
    # -----------------------------------------------------------
    def get_quote(self, symbol):
        resp = requests.get(
            f"{self.MARKET_DATA_URL}/{symbol}/quotes",
            headers=self._headers(),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json().get(symbol, {})
        return data

    def get_price_history(self, symbol, period_type="day", period=5,
                          frequency_type="minute", frequency=30):
        """
        Get historical bars.

        period_type: day, month, year, ytd
        period: # of periods
        frequency_type: minute, daily, weekly, monthly
        frequency: 1, 5, 10, 15, 30 (for minute); 1 for others
        """
        params = {
            "symbol": symbol,
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": "true"
        }
        resp = requests.get(
            f"{self.MARKET_DATA_URL}/pricehistory",
            headers=self._headers(),
            params=params,
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])

    def get_option_chain(self, symbol, contract_type="CALL", target_date=None):
        """
        Get options chain for a symbol.
        contract_type: CALL or PUT
        target_date: "YYYY-MM-DD" for specific expiration
        """
        params = {
            "symbol": symbol,
            "contractType": contract_type,
            "includeUnderlyingQuote": "true",
            "strategy": "SINGLE"
        }
        if target_date:
            params["fromDate"] = target_date
            params["toDate"] = target_date

        resp = requests.get(
            f"{self.MARKET_DATA_URL}/chains",
            headers=self._headers(),
            params=params,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        # Flatten the nested callExpDateMap / putExpDateMap into a simple list
        contracts = []
        key = "callExpDateMap" if contract_type == "CALL" else "putExpDateMap"
        for exp_date, strikes in data.get(key, {}).items():
            for strike, contract_list in strikes.items():
                for c in contract_list:
                    contracts.append({
                        "symbol": c.get("symbol"),
                        "strike": float(strike),
                        "expiration": exp_date.split(":")[0],
                        "bid": c.get("bid"),
                        "ask": c.get("ask"),
                        "last": c.get("last"),
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                        "iv": c.get("volatility"),
                        "volume": c.get("totalVolume"),
                        "open_interest": c.get("openInterest")
                    })
        return {"underlying": data.get("underlying", {}), "contracts": contracts}
