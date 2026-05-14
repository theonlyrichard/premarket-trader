from unittest.mock import call
import app as flask_app

FAKE_CANDLES = [
    {"datetime": 1715000000000, "open": 588.1, "high": 589.4,
     "low": 587.8, "close": 589.0, "volume": 123456},
    {"datetime": 1715001800000, "open": 589.0, "high": 590.1,
     "low": 588.5, "close": 589.8, "volume": 98765},
]


def test_chart_returns_intraday_and_daily(client):
    flask_app.schwab.get_price_history.return_value = FAKE_CANDLES

    resp = client.get("/api/chart/SPY")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["symbol"] == "SPY"
    assert data["intraday"] == FAKE_CANDLES
    assert data["daily"] == FAKE_CANDLES
    assert flask_app.schwab.get_price_history.call_count == 2

    calls = flask_app.schwab.get_price_history.call_args_list
    assert calls[0] == call("SPY", period_type="day", period=5, frequency_type="minute", frequency=30)
    assert calls[1] == call("SPY", period_type="month", period=1, frequency_type="daily", frequency=1)


def test_chart_qqq_also_works(client):
    flask_app.schwab.get_price_history.return_value = FAKE_CANDLES

    resp = client.get("/api/chart/QQQ")

    assert resp.status_code == 200
    assert resp.get_json()["symbol"] == "QQQ"
    assert flask_app.schwab.get_price_history.call_count == 2


def test_chart_rejects_unknown_symbol(client):
    resp = client.get("/api/chart/AAPL")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_chart_handles_schwab_error(client):
    flask_app.schwab.get_price_history.side_effect = RuntimeError("API down")

    resp = client.get("/api/chart/SPY")

    assert resp.status_code == 500
    assert "error" in resp.get_json()
