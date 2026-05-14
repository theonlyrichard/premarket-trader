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


@pytest.fixture(autouse=True)
def reset_schwab_mock():
    import app as _app_module
    _app_module.schwab.get_price_history.reset_mock()
    _app_module.schwab.get_price_history.side_effect = None


@pytest.fixture
def client(flask_app):
    with flask_app.test_client() as c:
        yield c
