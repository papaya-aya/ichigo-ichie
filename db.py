"""PostgreSQL connection + schema/seed helpers for the Ichigo Ichie shift manager.

Uses a thin sqlite3-compatible wrapper so app.py needs no changes:
  - conn.execute(sql, params)  — ? placeholders are auto-converted to %s
  - conn.executemany(sql, rows)
  - conn.commit() / conn.close()
  - rows returned as dict-like objects (keyed by column name)
"""
import os
import re
from datetime import datetime

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash as _gph


def hash_password(password):
    return _gph(password, method="pbkdf2:sha256")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Default owner password — change it from the owner dashboard after first login.
DEFAULT_OWNER_PASSWORD = "ichigo-admin"

# Seeded weekly production shifts. weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4.
SEED_TEMPLATES = [
    (0, "Monday production",    "06:45", "08:30",  36, 3, 3),
    (1, "Tuesday production",   "06:45", "09:30",  86, 4, 5),
    (2, "Wednesday production", "14:30", "17:30",  85, 4, 5),
    (4, "Friday production",    "06:45", "09:30", 102, 5, 7),
]

# Flavors as (column suffix, display label), used across orders + UI.
FLAVORS = [
    ("original", "Original"),
    ("matcha",   "Matcha"),
    ("hojicha",  "Hojicha"),
    ("other",    "Other"),
]

SEED_CLIENTS = [
    "Asha", "Iyasare", "Shoji", "Sushinista", "BWT", "Teance", "Yanagisawa",
]

DEFAULT_TARGET_PRODUCTIVITY = "6.5"

# Non-labour costs for Feb–Aug 2026, categorised from the Mercury and Amex
# exports (Gusto excluded — payroll is computed from shifts, not seeded here).
NON_LABOUR_COSTS_2026 = [
    ("2026-02", "Software & fees", "Relay Financial", 90.77),
    ("2026-02", "Insurance", "Next Insurance", 36.42),
    ("2026-02", "Software & fees", "INTUIT *", 19.00),
    ("2026-02", "Uncategorised", "VENMO", 1.00),
    ("2026-02", "Uncategorised", "INTUIT INC", 0.08),
    ("2026-03", "Kitchen rent", "PASSIONE PIZZA L", 1575.00),
    ("2026-03", "Insurance", "AmTrust Financial", 719.90),
    ("2026-03", "Uncategorised", "Berkeley Building Permit", 519.00),
    ("2026-03", "Ingredients", "Amex — Groceries", 330.18),
    ("2026-03", "Uncategorised", "Amex — Internet Purchase", 114.06),
    ("2026-03", "Ingredients", "Tokyo Fish Market", 75.00),
    ("2026-03", "Insurance", "Next Insurance", 36.42),
    ("2026-03", "Software & fees", "OPC Utilities Service Fee", 21.59),
    ("2026-03", "Software & fees", "INTUIT *", 19.00),
    ("2026-03", "Uncategorised", "Amex — Office Supplies", 12.82),
    ("2026-04", "Kitchen rent", "PASSIONE PIZZA L", 1575.00),
    ("2026-04", "Ingredients", "Kinokuniya", 1040.00),
    ("2026-04", "Ingredients", "Amex — Groceries", 1007.45),
    ("2026-04", "Uncategorised", "Amex — Internet Purchase", 109.97),
    ("2026-04", "Uncategorised", "Amex — Office Supplies", 80.41),
    ("2026-04", "Uncategorised", "VENMO", 69.78),
    ("2026-04", "Insurance", "Next Insurance", 36.42),
    ("2026-04", "Software & fees", "INTUIT *", 19.00),
    ("2026-05", "Kitchen rent", "PASSIONE PIZZA L", 1880.00),
    ("2026-05", "Ingredients", "Amex — Groceries", 862.42),
    ("2026-05", "Uncategorised", "Amex — Office Supplies", 175.38),
    ("2026-05", "Software & fees", "INTUIT *", 38.00),
    ("2026-05", "Insurance", "Next Insurance", 36.42),
    ("2026-05", "Uncategorised", "Amex — Restaurant", 8.92),
    ("2026-05", "Uncategorised", "Amex — Mail Order", 8.30),
    ("2026-06", "Ingredients", "Amex — Groceries", 489.14),
    ("2026-06", "Uncategorised", "Amex — Internet Purchase", 118.83),
    ("2026-06", "Software & fees", "INTUIT *", 38.00),
    ("2026-06", "Insurance", "Next Insurance", 36.42),
    ("2026-06", "Uncategorised", "Amex — Mail Order", 8.30),
    ("2026-07", "Kitchen rent", "PASSIONE PIZZA L", 1550.00),
    ("2026-07", "Ingredients", "Amex — Groceries", 646.81),
    ("2026-07", "Uncategorised", "Venmo", 101.90),
    ("2026-07", "Ingredients", "Amex — Wholesale Stores", 38.35),
    ("2026-07", "Software & fees", "QuickBooks", 38.00),
    ("2026-07", "Insurance", "Next Insurance", 36.42),
    ("2026-07", "Uncategorised", "Amex — Mail Order", 8.30),
    ("2026-08", "Ingredients", "Amex — Groceries", 277.24),
    ("2026-08", "Uncategorised", "Amex — Education", 116.95),
    ("2026-08", "Uncategorised", "Amex — Internet Purchase", 116.08),
    ("2026-08", "Insurance", "Next Insurance", 36.38),
    ("2026-08", "Uncategorised", "Sakurako Yanagisawa", 20.96),
    ("2026-08", "Uncategorised", "Amex — Mail Order", 16.60),
]



# ---------------------------------------------------------------------------
# sqlite3-compatible wrapper around psycopg2
# ---------------------------------------------------------------------------

def _to_pg(sql):
    """Convert SQLite ? placeholders to PostgreSQL %s."""
    return sql.replace("?", "%s")


class _Cursor:
    """Wraps a psycopg2 RealDictCursor to look like a sqlite3 cursor."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        # Rows affected by the last statement. INSERT ... ON CONFLICT DO
        # NOTHING reports 0 when it skipped, which is how callers count
        # what they actually created.
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # Not used in this app, but provided for completeness.
        return self._cur.fetchone()[0] if self._cur.rowcount else None


class _Connection:
    """Wraps a psycopg2 connection to expose the sqlite3 connection API."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_to_pg(sql), params)
        return _Cursor(cur)

    def executemany(self, sql, params_list):
        cur = self._conn.cursor()
        cur.executemany(_to_pg(sql), params_list)
        return _Cursor(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def get_db():
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", ""),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "postgres"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        sslmode="require",
    )
    conn.autocommit = False
    conn.set_client_encoding("UTF8")
    return _Connection(conn)


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

def migrate_db():
    """Apply one-time schema migrations that ALTER existing tables."""
    conn = get_db()
    # 2026-06-29: inventory columns changed from INTEGER → TEXT so managers
    # can enter free-form notes (numbers, fractions, Japanese, etc.).
    row = conn.execute(
        """SELECT data_type FROM information_schema.columns
            WHERE table_name='shift_reports' AND column_name='strawberry_stock'"""
    ).fetchone()
    if row and row["data_type"] == "integer":
        conn.execute(
            """ALTER TABLE shift_reports
                 ALTER COLUMN strawberry_stock TYPE TEXT USING strawberry_stock::TEXT,
                 ALTER COLUMN anko_stock       TYPE TEXT USING anko_stock::TEXT"""
        )
        conn.commit()

    # 2026-07-01: make submitted_by nullable so the owner can write reports
    # without an employee ID.
    null_row = conn.execute(
        """SELECT is_nullable FROM information_schema.columns
            WHERE table_name='shift_reports' AND column_name='submitted_by'"""
    ).fetchone()
    if null_row and null_row["is_nullable"] == "NO":
        conn.execute(
            "ALTER TABLE shift_reports ALTER COLUMN submitted_by DROP NOT NULL"
        )
        conn.commit()

    # 2026-07-05: rename clients to match Mercury system names.
    conn.execute("UPDATE clients SET name='Blue Willow Teaspot' WHERE name='BWT'")
    conn.execute("UPDATE clients SET name='Asha Tea' WHERE name='Asha'")
    # 2026-07-31: Shoji self-picks up — no driver needed.
    conn.execute("UPDATE clients SET default_deliverer='pick-up' WHERE name='Shoji'")
    # 2026-08-11: Asha Tea and Thao sell on consignment — sales are reported
    # monthly by the client rather than derived from delivered quantity.
    conn.execute(
        "UPDATE clients SET is_consignment=1"
        " WHERE name IN ('Asha Tea', 'Asha', 'Thao', 'Teance')"
    )
    conn.commit()

    # 2026-08-07: move Asha Tea's remaining August deliveries to Thursdays (one-time).
    if not conn.execute(
        "SELECT 1 FROM settings WHERE key='asha_thursdays_aug_2026'"
    ).fetchone():
        from datetime import date as _date, timedelta as _td
        asha = conn.execute(
            "SELECT id FROM clients WHERE name='Asha Tea'"
        ).fetchone()
        if asha:
            orders = conn.execute(
                """SELECT id, COALESCE(delivery_date, date) AS deliver_on
                     FROM orders
                    WHERE client_id = ?
                      AND COALESCE(delivery_date, date) > '2026-08-07'
                      AND COALESCE(delivery_date, date) <= '2026-08-31'
                      AND (is_pickup IS NULL OR is_pickup = 0)
                      AND (delivered IS NULL OR delivered = 0)""",
                (asha["id"],),
            ).fetchall()
            for o in orders:
                d = _date.fromisoformat(o["deliver_on"])
                monday = d - _td(days=d.weekday())
                thursday = monday + _td(days=3)
                # Last Thursday in August is Aug 27; cap anything that spills into Sep
                if thursday.month > 8:
                    thursday = _date(2026, 8, 27)
                conn.execute(
                    "UPDATE orders SET delivery_date = ? WHERE id = ?",
                    (thursday.isoformat(), o["id"]),
                )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('asha_thursdays_aug_2026', '1')"
            " ON CONFLICT (key) DO NOTHING"
        )
        conn.commit()

    # 2026-08-16: seed non-labour costs for Feb–Aug 2026 from the Mercury and
    # Amex exports, so the monthly summary shows a full cost picture (one-time;
    # the owner can edit or delete any of these from the Summary page).
    if not conn.execute(
        "SELECT 1 FROM settings WHERE key='seed_non_labour_2026'"
    ).fetchone():
        for month, category, label, amount in NON_LABOUR_COSTS_2026:
            conn.execute(
                """INSERT INTO summary_items
                     (month, kind, category, label, amount, note, created_at)
                   VALUES (?, 'cost', ?, ?, ?, 'imported from Mercury/Amex', ?)""",
                (month, category, label, amount, now_iso()),
            )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('seed_non_labour_2026', '1')"
            " ON CONFLICT (key) DO NOTHING"
        )
        conn.commit()

    # 2026-08-26: the owner's Expense & Sales Ledger itemises every card and
    # cash purchase, so the aggregated Amex rows seeded from the bank exports
    # are the same money counted twice. Drop those and let the ledger own them.
    # Bank ACH items the ledger never records — kitchen rent, insurance,
    # permits, software — are kept.
    if not conn.execute(
        "SELECT 1 FROM settings WHERE key='dedupe_ledger_overlap'"
    ).fetchone():
        for prefix in ("Amex — ", "Kinokuniya", "Tokyo Fish Market",
                       "VENMO", "Venmo", "Sakurako Yanagisawa"):
            conn.execute(
                """DELETE FROM summary_items
                    WHERE note = 'imported from Mercury/Amex'
                      AND label LIKE ?""",
                (prefix + "%",),
            )
        conn.execute(
            "INSERT INTO settings (key, value)"
            " VALUES ('dedupe_ledger_overlap', '1')"
            " ON CONFLICT (key) DO NOTHING"
        )
        conn.commit()

    # 2026-07-31: remove Teance from all August 2026 deliveries (one-time).
    if not conn.execute(
        "SELECT 1 FROM settings WHERE key='cleanup_teance_aug_2026'"
    ).fetchone():
        conn.execute(
            """DELETE FROM orders
                WHERE client_id = (SELECT id FROM clients WHERE name = 'Teance')
                  AND COALESCE(delivery_date, orders.date) LIKE ?""",
            ("2026-08-%",),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('cleanup_teance_aug_2026', '1')"
            " ON CONFLICT (key) DO NOTHING"
        )
        conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Schema init + seed
# ---------------------------------------------------------------------------

def init_db():
    """Create tables (if missing) and seed default data once."""
    conn = get_db()

    # Run schema — split on semicolons and execute each statement individually.
    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()

    statements = [s.strip() for s in schema.split(";") if s.strip()]
    for stmt in statements:
        conn.execute(stmt)

    # Seed settings
    _seed_setting(conn, "slack_webhook",         "")
    _seed_setting(conn, "owner_password_hash",   hash_password(DEFAULT_OWNER_PASSWORD))
    _seed_setting(conn, "target_productivity",   DEFAULT_TARGET_PRODUCTIVITY)
    _seed_setting(conn, "piece_rate",            "2.00")
    _seed_setting(conn, "gusto_rate",            "20.00")
    _seed_setting(conn, "strawberry_price",      "10.00")
    _seed_setting(conn, "delivery_transport",    "6.00")
    _seed_setting(conn, "popup_rate",            "7.20")
    _seed_setting(conn, "pickup_rate",           "6.50")
    _seed_setting(conn, "popup_waste",           "0.10")
    _seed_setting(conn, "mercury_api_key",       "")

    # Seed shift templates
    if conn.execute("SELECT COUNT(*) AS n FROM shift_templates").fetchone()["n"] == 0:
        conn.executemany(
            """INSERT INTO shift_templates
               (weekday, label, start_time, end_time, quantity, min_people, max_people)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            SEED_TEMPLATES,
        )

    # Seed clients
    if conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"] == 0:
        conn.executemany("INSERT INTO clients (name) VALUES (%s)",
                         [(c,) for c in SEED_CLIENTS])

    conn.commit()
    conn.close()


def _seed_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Helpers used by app.py
# ---------------------------------------------------------------------------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def now_iso():
    return datetime.now().isoformat(timespec="seconds")
