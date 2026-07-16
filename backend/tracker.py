"""
Outcome tracker + trading journal.

Logs scanner recommendations, resolves win/loss from later price data,
and stores human journal tags (took / passed / FOMO / late) + free notes.

Empirical win% only — never fabricated probability.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Valid journal decisions for analytics
JOURNAL_DECISIONS = {
    "took",
    "passed",
    "fomo",
    "late",
    "managed",
    "skipped_macro",
    "other",
    "",
    None,
}


class OutcomeTracker:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
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
                raw_setup TEXT,
                pattern TEXT,
                zone_timeframe TEXT,
                session_phase TEXT,
                expiry_style TEXT,
                agency REAL,
                journal_decision TEXT,
                journal_note TEXT,
                journal_at TEXT,
                journal_rating INTEGER
            )
            """
        )
        # Migrate older DBs that lack new columns
        existing = {
            r[1] for r in conn.execute("PRAGMA table_info(recommendations)").fetchall()
        }
        for col, decl in [
            ("pattern", "TEXT"),
            ("zone_timeframe", "TEXT"),
            ("session_phase", "TEXT"),
            ("expiry_style", "TEXT"),
            ("agency", "REAL"),
            ("journal_decision", "TEXT"),
            ("journal_note", "TEXT"),
            ("journal_at", "TEXT"),
            ("journal_rating", "INTEGER"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {decl}")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcome ON recommendations(outcome)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recorded_at ON recommendations(recorded_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal ON recommendations(journal_decision)"
        )
        conn.commit()
        conn.close()

    def record_recommendation(self, analysis):
        """Record a new recommendation (dedupe by symbol + date + zone)."""
        setup = analysis.get("setup")
        if not setup:
            return None

        today = datetime.now(timezone.utc).date().isoformat()
        conn = self._conn()

        existing = conn.execute(
            """
            SELECT id FROM recommendations
            WHERE symbol = ? AND direction = ?
              AND ABS(zone_top - ?) < 0.5 AND ABS(zone_bottom - ?) < 0.5
              AND DATE(recorded_at) = ?
            """,
            (
                setup["instrument"],
                setup["direction"],
                setup["zone_top"],
                setup["zone_bottom"],
                today,
            ),
        ).fetchone()

        if existing:
            conn.close()
            return existing["id"]

        entry_price = setup.get("proximal") or (
            setup.get("zone_top")
            if setup["direction"] == "CALL"
            else setup.get("zone_bottom")
        )

        cur = conn.execute(
            """
            INSERT INTO recommendations
            (recorded_at, symbol, direction, current_price, entry_trigger_price,
             stop, target, rr_ratio, conviction_score, conviction_label,
             zone_top, zone_bottom, confluence_factors, macro_hostile, raw_setup,
             pattern, zone_timeframe, session_phase, expiry_style, agency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                json.dumps(setup, default=str),
                setup.get("pattern") or setup.get("playbook_label"),
                setup.get("zone_timeframe"),
                setup.get("session_phase") or setup.get("session_label"),
                setup.get("expiry_style"),
                setup.get("agency") or setup.get("zone_strength"),
            ),
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return new_id

    def check_outcomes(self, schwab_client):
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT * FROM recommendations
            WHERE outcome = 'open'
              AND DATE(recorded_at) < DATE('now')
            ORDER BY recorded_at
            """
        ).fetchall()

        for row in rows:
            try:
                bars = schwab_client.get_price_history(
                    row["symbol"],
                    period_type="day",
                    period=5,
                    frequency_type="minute",
                    frequency=5,
                )
                recorded_dt = datetime.fromisoformat(row["recorded_at"])
                if recorded_dt.tzinfo is None:
                    recorded_dt = recorded_dt.replace(tzinfo=timezone.utc)
                recorded_ms = recorded_dt.timestamp() * 1000

                outcome = None
                outcome_time = None

                for bar in bars:
                    if bar.get("datetime", 0) < recorded_ms:
                        continue
                    if row["direction"] == "CALL":
                        if bar["high"] >= row["target"]:
                            outcome = "win"
                            outcome_time = bar["datetime"]
                            break
                        if bar["low"] <= row["stop"]:
                            outcome = "loss"
                            outcome_time = bar["datetime"]
                            break
                    else:
                        if bar["low"] <= row["target"]:
                            outcome = "win"
                            outcome_time = bar["datetime"]
                            break
                        if bar["high"] >= row["stop"]:
                            outcome = "loss"
                            outcome_time = bar["datetime"]
                            break

                age_days = (datetime.now(timezone.utc) - recorded_dt).days
                if not outcome and age_days > 5:
                    outcome = "expired"

                if outcome:
                    conn.execute(
                        """
                        UPDATE recommendations
                        SET outcome = ?, outcome_at = ?
                        WHERE id = ?
                        """,
                        (
                            outcome,
                            (
                                datetime.fromtimestamp(
                                    outcome_time / 1000, tz=timezone.utc
                                ).isoformat()
                                if outcome_time
                                else None
                            ),
                            row["id"],
                        ),
                    )
            except Exception as e:
                print(f"Outcome check failed for rec {row['id']}: {e}")

        conn.commit()
        conn.close()

    def get_rolling_stats(self, days=30):
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT outcome, conviction_label, COUNT(*) as n
            FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
            GROUP BY outcome, conviction_label
            """,
            (days,),
        ).fetchall()

        stats = {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "open": 0,
            "expired": 0,
            "by_conviction": {
                "High": {"w": 0, "l": 0},
                "Medium": {"w": 0, "l": 0},
                "Low": {"w": 0, "l": 0},
            },
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
        stats["win_rate"] = (
            round(stats["wins"] / closed * 100, 1) if closed else None
        )
        stats["days"] = days

        for label, counts in stats["by_conviction"].items():
            total = counts["w"] + counts["l"]
            counts["win_rate"] = (
                round(counts["w"] / total * 100, 1) if total else None
            )
            counts["total"] = total

        # Taken trades only (journal took/managed)
        taken = conn.execute(
            """
            SELECT outcome, COUNT(*) as n FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
              AND journal_decision IN ('took', 'managed', 'late', 'fomo')
            GROUP BY outcome
            """,
            (days,),
        ).fetchall()
        tw = tl = 0
        for row in taken:
            if row["outcome"] == "win":
                tw += row["n"]
            elif row["outcome"] == "loss":
                tl += row["n"]
        tclosed = tw + tl
        stats["taken"] = {
            "wins": tw,
            "losses": tl,
            "closed": tclosed,
            "win_rate": round(tw / tclosed * 100, 1) if tclosed else None,
        }

        conn.close()
        return stats

    def list_journal(self, limit=50, days=90):
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT id, recorded_at, symbol, direction, current_price,
                   entry_trigger_price, stop, target, rr_ratio,
                   conviction_score, conviction_label, outcome, outcome_at,
                   pattern, zone_timeframe, session_phase, expiry_style, agency,
                   macro_hostile, journal_decision, journal_note, journal_at,
                   journal_rating, zone_top, zone_bottom
            FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (days, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_journal(self, rec_id, decision=None, note=None, rating=None):
        """
        Attach human journal metadata to a recommendation.
        decision: took | passed | fomo | late | managed | skipped_macro | other
        rating: 1–5 optional self grade on process (not P&L)
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"error": "not_found"}

        if decision is not None:
            d = (decision or "").strip().lower()
            if d and d not in {
                "took",
                "passed",
                "fomo",
                "late",
                "managed",
                "skipped_macro",
                "other",
            }:
                conn.close()
                return {"error": "invalid_decision"}
        else:
            d = None

        # Build update
        fields = []
        vals = []
        if d is not None:
            fields.append("journal_decision = ?")
            vals.append(d if d else None)
        if note is not None:
            fields.append("journal_note = ?")
            vals.append(note[:2000] if note else None)
        if rating is not None:
            try:
                r = int(rating)
                if r < 1 or r > 5:
                    raise ValueError
            except Exception:
                conn.close()
                return {"error": "invalid_rating"}
            fields.append("journal_rating = ?")
            vals.append(r)

        if not fields:
            conn.close()
            return {"error": "nothing_to_update"}

        fields.append("journal_at = ?")
        vals.append(datetime.now(timezone.utc).isoformat())
        vals.append(rec_id)

        conn.execute(
            f"UPDATE recommendations SET {', '.join(fields)} WHERE id = ?",
            vals,
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        conn.close()
        return {"ok": True, "row": dict(updated)}

    def journal_analytics(self, days=60):
        """
        Breakdown by pattern / TF / decision / conviction.
        win_rate only when closed outcomes exist — never invented.
        """
        conn = self._conn()

        def barrier(sql, params=()):
            rows = conn.execute(sql, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                w = d.get("wins") or 0
                l = d.get("losses") or 0
                closed = w + l
                d["closed"] = closed
                d["win_rate"] = round(w / closed * 100, 1) if closed else None
                out.append(d)
            return out

        base = """
            SELECT {dim} as bucket,
                   COUNT(*) as n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   SUM(CASE WHEN journal_decision IN ('took','managed','late','fomo') THEN 1 ELSE 0 END) as taken_n,
                   SUM(CASE WHEN journal_decision='passed' THEN 1 ELSE 0 END) as passed_n
            FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
            GROUP BY {dim}
            ORDER BY n DESC
        """

        by_pattern = barrier(
            base.format(dim="COALESCE(NULLIF(pattern,''), 'unknown')"),
            (days,),
        )
        by_tf = barrier(
            base.format(dim="COALESCE(NULLIF(zone_timeframe,''), 'unknown')"),
            (days,),
        )
        by_decision = barrier(
            base.format(dim="COALESCE(NULLIF(journal_decision,''), 'unlogged')"),
            (days,),
        )
        by_conviction = barrier(
            base.format(dim="COALESCE(NULLIF(conviction_label,''), 'unknown')"),
            (days,),
        )
        by_session = barrier(
            base.format(dim="COALESCE(NULLIF(session_phase,''), 'unknown')"),
            (days,),
        )
        by_symbol = barrier(
            base.format(dim="symbol"),
            (days,),
        )

        # Process grade average when ratings exist
        rating_row = conn.execute(
            """
            SELECT AVG(journal_rating) as avg_rating, COUNT(journal_rating) as n
            FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
              AND journal_rating IS NOT NULL
            """,
            (days,),
        ).fetchone()

        unjournaled = conn.execute(
            """
            SELECT COUNT(*) as n FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
              AND (journal_decision IS NULL OR journal_decision = '')
            """,
            (days,),
        ).fetchone()["n"]

        total = conn.execute(
            """
            SELECT COUNT(*) as n FROM recommendations
            WHERE DATE(recorded_at) >= DATE('now', '-' || ? || ' days')
            """,
            (days,),
        ).fetchone()["n"]

        conn.close()
        return {
            "days": days,
            "total_recs": total,
            "unjournaled": unjournaled,
            "avg_process_rating": (
                round(rating_row["avg_rating"], 2)
                if rating_row and rating_row["avg_rating"] is not None
                else None
            ),
            "ratings_count": (rating_row["n"] if rating_row else 0),
            "by_pattern": by_pattern,
            "by_timeframe": by_tf,
            "by_decision": by_decision,
            "by_conviction": by_conviction,
            "by_session": by_session,
            "by_symbol": by_symbol,
            "note": (
                "Win rates need closed outcomes. Journal tags (took/passed/fomo/late) "
                "are for process — only win/loss after price resolution is edge truth."
            ),
        }
