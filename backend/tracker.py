"""
Silent outcome tracker.

Logs every recommendation made by the scanner into a local SQLite database.
Each time the scanner runs, it:
  1. Checks any "open" past recommendations to see if target or stop was hit
     using subsequent price data from Schwab
  2. Records new recommendations
  3. Returns rolling win/loss stats for display at the top of the dashboard

The user never interacts with this directly. It just accumulates truth about
whether the scanner's recommendations are working.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class OutcomeTracker:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                current_price REAL NOT NULL,
                entry_trigger_price REAL,
                stop REAL NOT NULL,
                target REAL NOT NULL,
                rr_ratio REAL,
                conviction_score INTEGER,
                conviction_label TEXT,
                zone_top REAL,
                zone_bottom REAL,
                confluence_factors TEXT,
                macro_hostile INTEGER,
                outcome TEXT DEFAULT 'open',
                outcome_at TEXT,
                raw_setup TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcome ON recommendations(outcome)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recorded_at ON recommendations(recorded_at)")
        conn.commit()
        conn.close()

    def record_recommendation(self, analysis):
        """Record a new recommendation (dedupe by symbol + date + zone)."""
        setup = analysis.get("setup")
        if not setup:
            return

        today = datetime.now(timezone.utc).date().isoformat()
        conn = sqlite3.connect(self.db_path)

        # Dedupe: don't record the same setup twice in one day
        existing = conn.execute("""
            SELECT id FROM recommendations
            WHERE symbol = ? AND direction = ?
              AND ABS(zone_top - ?) < 0.5 AND ABS(zone_bottom - ?) < 0.5
              AND DATE(recorded_at) = ?
        """, (setup["instrument"], setup["direction"],
              setup["zone_top"], setup["zone_bottom"], today)).fetchone()

        if existing:
            conn.close()
            return

        # Extract entry trigger price from the string
        entry_price = setup.get("zone_top") if setup["direction"] == "CALL" else setup.get("zone_bottom")

        conn.execute("""
            INSERT INTO recommendations
            (recorded_at, symbol, direction, current_price, entry_trigger_price,
             stop, target, rr_ratio, conviction_score, conviction_label,
             zone_top, zone_bottom, confluence_factors, macro_hostile, raw_setup)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            setup["instrument"],
            setup["direction"],
            setup["current_price"],
            entry_price,
            setup["stop"],
            setup["target"],
            setup["rr_ratio"],
            setup["conviction_score"],
            setup["conviction_label"],
            setup["zone_top"],
            setup["zone_bottom"],
            json.dumps(setup.get("confluence_factors", [])),
            1 if setup.get("macro", {}).get("hostile") else 0,
            json.dumps(setup, default=str)
        ))
        conn.commit()
        conn.close()

    def check_outcomes(self, schwab_client):
        """
        For each 'open' recommendation older than 1 day, check if the
        subsequent price action hit target or stop.

        Call this periodically (e.g., once per scan) to keep outcomes updated.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM recommendations
            WHERE outcome = 'open'
              AND DATE(recorded_at) < DATE('now')
            ORDER BY recorded_at
        """).fetchall()

        for row in rows:
            try:
                # Pull bars since the recommendation was made
                bars = schwab_client.get_price_history(
                    row["symbol"],
                    period_type="day", period=5,
                    frequency_type="minute", frequency=5
                )
                recorded_dt = datetime.fromisoformat(row["recorded_at"])
                recorded_ms = recorded_dt.timestamp() * 1000

                outcome = None
                outcome_time = None

                for bar in bars:
                    if bar.get("datetime", 0) < recorded_ms:
                        continue
                    if row["direction"] == "CALL":
                        if bar["high"] >= row["target"]:
                            outcome = "win"; outcome_time = bar["datetime"]; break
                        if bar["low"] <= row["stop"]:
                            outcome = "loss"; outcome_time = bar["datetime"]; break
                    else:  # PUT
                        if bar["low"] <= row["target"]:
                            outcome = "win"; outcome_time = bar["datetime"]; break
                        if bar["high"] >= row["stop"]:
                            outcome = "loss"; outcome_time = bar["datetime"]; break

                # If it's been more than 5 days with no resolution, mark expired
                age_days = (datetime.now(timezone.utc) - recorded_dt).days
                if not outcome and age_days > 5:
                    outcome = "expired"

                if outcome:
                    conn.execute("""
                        UPDATE recommendations
                        SET outcome = ?, outcome_at = ?
                        WHERE id = ?
                    """, (outcome,
                          datetime.fromtimestamp(outcome_time / 1000, tz=timezone.utc).isoformat() if outcome_time else None,
                          row["id"]))
            except Exception as e:
                # Don't let tracker failures kill the scan
                print(f"Outcome check failed for rec {row['id']}: {e}")

        conn.commit()
        conn.close()

    def get_rolling_stats(self, days=30):
        """Return summary stats for display. Silent tracking surfaces here."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT outcome, conviction_label, COUNT(*) as n
            FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
            GROUP BY outcome, conviction_label
        """, (days,)).fetchall()

        stats = {
            "total": 0, "wins": 0, "losses": 0, "open": 0, "expired": 0,
            "by_conviction": {"High": {"w": 0, "l": 0}, "Medium": {"w": 0, "l": 0}, "Low": {"w": 0, "l": 0}}
        }

        for row in rows:
            stats["total"] += row["n"]
            if row["outcome"] == "win":
                stats["wins"] += row["n"]
                if row["conviction_label"] in stats["by_conviction"]:
                    stats["by_conviction"][row["conviction_label"]]["w"] += row["n"]
            elif row["outcome"] == "loss":
                stats["losses"] += row["n"]
                if row["conviction_label"] in stats["by_conviction"]:
                    stats["by_conviction"][row["conviction_label"]]["l"] += row["n"]
            elif row["outcome"] == "open":
                stats["open"] += row["n"]
            elif row["outcome"] == "expired":
                stats["expired"] += row["n"]

        closed = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / closed * 100, 1) if closed else None
        stats["days"] = days

        # Per-conviction win rates (what the user actually needs to see)
        for label, counts in stats["by_conviction"].items():
            total = counts["w"] + counts["l"]
            counts["win_rate"] = round(counts["w"] / total * 100, 1) if total else None
            counts["total"] = total

        conn.close()
        return stats
