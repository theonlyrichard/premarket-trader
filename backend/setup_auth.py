"""
One-time Schwab OAuth setup script.

Run this ONCE after getting your Schwab app_key and app_secret approved.
After this, the main app (app.py) will auto-refresh tokens and you won't
need to run this again unless your refresh token expires (typically 7 days
unless you re-authenticate).

Usage:
    cd backend
    python setup_auth.py
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from schwab_client import SchwabClient

CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

if not CONFIG_PATH.exists():
    print("ERROR: config.json not found.")
    print(f"Copy {BASE_DIR / 'config.example.json'} to config.json and fill in your keys.")
    sys.exit(1)

with open(CONFIG_PATH) as f:
    config = json.load(f)

schwab = SchwabClient(
    app_key=config["schwab"]["app_key"],
    app_secret=config["schwab"]["app_secret"],
    callback_url=config["schwab"]["callback_url"],
    token_path=DATA_DIR / "schwab_tokens.json"
)

print("=" * 60)
print("Schwab OAuth Setup")
print("=" * 60)
print()
print("Step 1: Open this URL in your browser:")
print()
print(schwab.get_auth_url())
print()
print("Step 2: Log in with your Schwab brokerage credentials.")
print("Step 3: Approve the app. You'll be redirected to a URL that")
print("        starts with https://127.0.0.1/?code=...")
print("        Your browser will show 'site can't be reached' — that's fine.")
print("Step 4: Copy the ENTIRE URL from the browser address bar.")
print()
callback_url = input("Paste the callback URL here: ").strip()

try:
    schwab.exchange_code(callback_url)
    print()
    print("Success! Tokens saved to data/schwab_tokens.json")
    print("You can now run: python app.py")
except Exception as e:
    print(f"\nERROR: {e}")
    print("Check that you pasted the complete URL including the code parameter.")
    sys.exit(1)
