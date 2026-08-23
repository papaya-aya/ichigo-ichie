"""Run the real Flask app against an in-memory SQLite database.

db.py talks to Postgres, so there is no way to exercise a page locally without
the production credentials. This installs a stand-in `db` module with the same
surface *before* app.py is imported, so pages can actually be rendered rather
than only syntax-checked.

It loads the real schema.sql, so a table or column added there is picked up
here automatically.

    ./venv/bin/python tests/smoke_test.py
"""
import os
import re
import sys
import sqlite3
import types
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_con = sqlite3.connect(":memory:", check_same_thread=False)
_con.row_factory = sqlite3.Row


def _pg_to_sqlite(sql):
    sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    return re.sub(r"\bNUMERIC\b", "REAL", sql)


class _Cur:
    def __init__(self, c):
        self._c = c

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def __iter__(self):
        return iter(self._c.fetchall())

    @property
    def lastrowid(self):
        return self._c.lastrowid


class _Conn:
    def execute(self, sql, params=()):
        return _Cur(_con.execute(_pg_to_sqlite(sql), params))

    def executemany(self, sql, rows):
        return _Cur(_con.executemany(_pg_to_sqlite(sql), rows))

    def commit(self):
        _con.commit()

    def close(self):
        pass


def _init():
    with open(os.path.join(ROOT, "schema.sql")) as f:
        schema = f.read()
    for raw in schema.split(";"):
        # strip leading comment lines so the statement keyword comes first
        stmt = "\n".join(
            l for l in raw.splitlines() if not l.strip().startswith("--")
        ).strip()
        if not stmt:
            continue
        if stmt.upper().startswith("ALTER TABLE"):
            stmt = stmt.replace("IF NOT EXISTS ", "")  # sqlite lacks it here
            try:
                _con.execute(_pg_to_sqlite(stmt))
            except sqlite3.OperationalError:
                pass  # column already exists
            continue
        _con.execute(_pg_to_sqlite(stmt))
    for k, v in [
        ("piece_rate", "2.00"), ("gusto_rate", "20.00"),
        ("strawberry_price", "10.00"), ("delivery_transport", "6.00"),
        ("target_productivity", "6.5"), ("popup_rate", "7.20"),
        ("pickup_rate", "6.50"), ("popup_waste", "0.10"),
        ("owner_password_hash", "x"), ("slack_webhook", ""),
        ("mercury_api_key", ""),
    ]:
        _con.execute(
            "INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))
    _con.commit()


stub = types.ModuleType("db")
stub.FLAVORS = [("original", "Original"), ("matcha", "Matcha"),
                ("hojicha", "Hojicha"), ("other", "Other")]
stub.DEFAULT_OWNER_PASSWORD = "x"
stub.DEFAULT_TARGET_PRODUCTIVITY = "6.5"
stub.SEED_TEMPLATES = []
stub.SEED_CLIENTS = []
stub.NON_LABOUR_COSTS_2026 = []
stub.hash_password = lambda p: "hash$" + p
stub.get_db = lambda: _Conn()
stub.now_iso = lambda: datetime.now().isoformat(timespec="seconds")
stub.get_setting = lambda c, k, d=None: (
    (lambda r: r["value"] if r else d)(
        c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()))
stub.set_setting = lambda c, k, v: c.execute(
    "INSERT INTO settings (key,value) VALUES (?,?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
stub.init_db = _init
stub.migrate_db = lambda: None
sys.modules["db"] = stub

import app as appmod                                          # noqa: E402

flask_app = appmod.app
flask_app.config.update(TESTING=True, SECRET_KEY="test")


def seed_fixture():
    """A September with shifts, orders, a pop-up, a delivery and purchase runs."""
    c = _con
    c.execute("INSERT INTO employees (id,name,pin_hash,active,created_at)"
              " VALUES (1,'Yumi','h',1,'x')")
    c.execute("INSERT INTO employees (id,name,pin_hash,active,created_at)"
              " VALUES (2,'Saku','h',1,'x')")
    c.execute("INSERT INTO clients (id,name,active) VALUES (1,'Iyasare',1)")
    c.execute("INSERT INTO clients (id,name,active) VALUES (2,'Shoji',1)")
    c.execute("UPDATE clients SET unit_price=5.20 WHERE id=1")
    c.execute("UPDATE clients SET unit_price=0, default_deliverer='pick-up'"
              " WHERE id=2")
    c.execute("INSERT INTO shift_templates (id,weekday,label,start_time,end_time,"
              "quantity,min_people,max_people)"
              " VALUES (1,2,'Wed production','06:45','09:30',80,2,4)")
    c.execute("INSERT INTO shift_instances (id,template_id,date)"
              " VALUES (1,1,'2026-09-02')")
    c.execute("INSERT INTO assignments (shift_instance_id,employee_id,start_time,"
              "end_time,is_manager) VALUES (1,1,'06:45','09:30',1)")
    c.execute("INSERT INTO assignments (shift_instance_id,employee_id,start_time,"
              "end_time,is_manager) VALUES (1,2,'06:45','09:30',0)")
    c.execute("INSERT INTO orders (client_id,date,delivery_date,qty_original,"
              "deliverer,created_at) VALUES (1,'2026-09-02','2026-09-02',200,'Yumi','x')")
    c.execute("INSERT INTO orders (client_id,date,delivery_date,is_pickup,"
              "qty_original,note,created_at)"
              " VALUES (2,'2026-09-05','2026-09-05',1,150,'Berkeley market','x')")
    c.execute("INSERT INTO purchase_instances (id,date,created_at)"
              " VALUES (1,'2026-09-02','x')")
    c.execute("INSERT INTO purchase_instances (id,date,created_at)"
              " VALUES (2,'2026-09-04','x')")
    c.execute("INSERT INTO purchase_availability"
              " (employee_id,purchase_instance_id,submitted_at) VALUES (1,1,'x')")
    c.execute("INSERT INTO purchase_assignments"
              " (purchase_instance_id,employee_id,completed,created_at)"
              " VALUES (1,1,1,'x')")
    # duplicate of the assigned run — must NOT be paid twice
    c.execute("INSERT INTO strawberry_purchases"
              " (employee_id,date,quantity,created_at,status)"
              " VALUES (1,'2026-09-02',1,'x','approved')")
    # genuine ad-hoc on a non-run date — must be paid
    c.execute("INSERT INTO strawberry_purchases"
              " (employee_id,date,quantity,created_at,status)"
              " VALUES (2,'2026-09-09',1,'x','approved')")
    c.execute("INSERT INTO summary_items"
              " (month,kind,category,label,amount,note,created_at)"
              " VALUES ('2026-09','cost','Ingredients','Berkeley Bowl',412.55,'','x')")
    c.execute("INSERT INTO summary_items"
              " (month,kind,category,label,amount,note,created_at)"
              " VALUES ('2026-09','revenue','Uncategorised','Ottimate',565.00,'','x')")
    c.commit()


def owner_client():
    cl = flask_app.test_client()
    with cl.session_transaction() as s:
        s["owner"] = True
    return cl
