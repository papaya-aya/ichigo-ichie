"""SQLite connection + schema/seed helpers for the Ichigo Ichie shift manager."""
import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash as _gph


def hash_password(password):
    # Use PBKDF2 explicitly: the macOS system-Python build lacks hashlib.scrypt,
    # which is Werkzeug's default.
    return _gph(password, method="pbkdf2:sha256")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# On Render, set DB_DIR=/data so the database survives deploys (persistent disk).
# Locally it stays next to the app.
_db_dir = os.environ.get("DB_DIR", BASE_DIR)
DB_PATH = os.path.join(_db_dir, "shifto.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# Default owner password — change it from the owner dashboard after first login.
DEFAULT_OWNER_PASSWORD = "ichigo-admin"

# Seeded weekly production shifts. weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4.
SEED_TEMPLATES = [
    # weekday, label,          start,   end,     qty, min, max
    (0, "Monday production",   "06:45", "08:30",  36, 3, 3),
    (1, "Tuesday production",  "06:45", "09:30",  86, 4, 5),
    (2, "Wednesday production","14:30", "17:30",  85, 4, 5),
    (4, "Friday production",   "06:45", "09:30", 102, 5, 7),
]

# Flavors as (column suffix, display label), used across orders + UI.
FLAVORS = [
    ("original", "Original"),
    ("matcha", "Matcha"),
    ("hojicha", "Hojicha"),
    ("other", "Other"),
]

# Clients seen in the existing production sheet — seeded so order entry is quick.
SEED_CLIENTS = [
    "Asha", "Iyasare", "Shoji", "Sushinista", "BWT", "Teance", "Yanagisawa",
]

# Target pieces per person-hour. Used to suggest team size from order volume.
# Observed in the sheet: roughly 5.5–7.3; 6.5 is a reasonable middle default.
DEFAULT_TARGET_PRODUCTIVITY = "6.5"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables (if missing) and seed owner password + shift templates once."""
    conn = get_db()
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())

    # Lightweight migrations: add columns to pre-existing tables (CREATE TABLE
    # IF NOT EXISTS won't alter an existing table).
    _ensure_column(conn, "orders", "delivery_date", "TEXT")
    _ensure_column(conn, "orders", "delivered", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_delivery ON orders(delivery_date)")
    _ensure_column(conn, "assignments", "is_manager",         "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "assignments", "actual_start",       "TEXT")
    _ensure_column(conn, "assignments", "actual_end",         "TEXT")
    _ensure_column(conn, "assignments", "strawberries_bought","INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "recurring_orders", "delivery_offset", "INTEGER NOT NULL DEFAULT 0")
    # Request/approval flow for employee-submitted entries
    _ensure_column(conn, "strawberry_purchases", "status",       "TEXT NOT NULL DEFAULT 'approved'")
    _ensure_column(conn, "strawberry_purchases", "requested_by", "INTEGER")
    _ensure_column(conn, "popups",               "status",       "TEXT NOT NULL DEFAULT 'approved'")
    _ensure_column(conn, "popups",               "requested_by", "INTEGER")
    # Delivery availability
    _ensure_column(conn, "availability", "can_deliver", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "availability", "is_update",   "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "popups", "start_time", "TEXT")
    _ensure_column(conn, "popups", "end_time",   "TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS delivery_blackout (date TEXT PRIMARY KEY)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS weekday_managers (
          weekday     INTEGER PRIMARY KEY,
          employee_id INTEGER REFERENCES employees(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shift_reports (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
          submitted_by      INTEGER NOT NULL REFERENCES employees(id),
          status            TEXT    NOT NULL DEFAULT 'pending',
          strawberry_stock  INTEGER,
          anko_stock        INTEGER,
          memo              TEXT,
          submitted_at      TEXT    NOT NULL,
          decided_at        TEXT,
          UNIQUE(shift_instance_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shift_report_hours (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          report_id    INTEGER NOT NULL REFERENCES shift_reports(id),
          employee_id  INTEGER NOT NULL REFERENCES employees(id),
          actual_start TEXT    NOT NULL,
          actual_end   TEXT    NOT NULL,
          UNIQUE(report_id, employee_id)
        )"""
    )
    if conn.execute("SELECT value FROM settings WHERE key='slack_webhook'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES ('slack_webhook', ?)",
                     ("",))

    cur = conn.execute("SELECT value FROM settings WHERE key = 'owner_password_hash'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("owner_password_hash", hash_password(DEFAULT_OWNER_PASSWORD)),
        )

    cur = conn.execute("SELECT COUNT(*) AS n FROM shift_templates")
    if cur.fetchone()["n"] == 0:
        conn.executemany(
            """INSERT INTO shift_templates
               (weekday, label, start_time, end_time, quantity, min_people, max_people)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            SEED_TEMPLATES,
        )

    if conn.execute("SELECT value FROM settings WHERE key = 'target_productivity'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                     ("target_productivity", DEFAULT_TARGET_PRODUCTIVITY))

    if conn.execute("SELECT value FROM settings WHERE key = 'piece_rate'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("piece_rate", "2.00"))

    if conn.execute("SELECT value FROM settings WHERE key = 'gusto_rate'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("gusto_rate", "20.00"))

    if conn.execute("SELECT value FROM settings WHERE key = 'strawberry_price'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("strawberry_price", "10.00"))

    if conn.execute("SELECT value FROM settings WHERE key = 'delivery_transport'").fetchone() is None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("delivery_transport", "6.00"))

    if conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"] == 0:
        conn.executemany("INSERT INTO clients (name) VALUES (?)",
                         [(c,) for c in SEED_CLIENTS])

    conn.commit()
    conn.close()


def _ensure_column(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now_iso():
    return datetime.now().isoformat(timespec="seconds")
