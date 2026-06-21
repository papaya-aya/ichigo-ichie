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
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return _Connection(conn)


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
