-- Ichigo Ichie shift manager — SQLite schema

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  pin_hash   TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- Weekly recurring production shifts. weekday uses Python's convention: Mon=0 .. Sun=6.
CREATE TABLE IF NOT EXISTS shift_templates (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  weekday    INTEGER NOT NULL,
  label      TEXT NOT NULL,
  start_time TEXT NOT NULL,   -- 'HH:MM'
  end_time   TEXT NOT NULL,   -- 'HH:MM'
  quantity   INTEGER NOT NULL,
  min_people INTEGER NOT NULL,
  max_people INTEGER NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1
);

-- A concrete dated occurrence of a template (e.g. Mon 2026-07-06).
CREATE TABLE IF NOT EXISTS shift_instances (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER NOT NULL REFERENCES shift_templates(id),
  date        TEXT NOT NULL,    -- 'YYYY-MM-DD'
  UNIQUE(template_id, date)
);

-- An employee's submitted availability for one shift instance (may be a partial window).
CREATE TABLE IF NOT EXISTS availability (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id       INTEGER NOT NULL REFERENCES employees(id),
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  start_time        TEXT NOT NULL,   -- 'HH:MM'
  end_time          TEXT NOT NULL,   -- 'HH:MM'
  status            TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
  note              TEXT,
  submitted_at      TEXT NOT NULL,
  decided_at        TEXT,
  UNIQUE(employee_id, shift_instance_id)
);

-- Who is actually scheduled on a shift instance, and for which window.
CREATE TABLE IF NOT EXISTS assignments (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  employee_id       INTEGER NOT NULL REFERENCES employees(id),
  start_time        TEXT NOT NULL,
  end_time          TEXT NOT NULL,
  UNIQUE(shift_instance_id, employee_id)
);

-- ---------------------------------------------------------------------------
-- Production / order tracking (Phase 1)
-- ---------------------------------------------------------------------------

-- Restaurants / cafes we sell to.
CREATE TABLE IF NOT EXISTS clients (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  name   TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);

-- One client's order for one production date, with quantities per flavor.
-- Flavors are fixed columns to mirror the spreadsheet's "O:8 M:8 H:30" style.
CREATE TABLE IF NOT EXISTS orders (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id     INTEGER NOT NULL REFERENCES clients(id),
  date          TEXT NOT NULL,            -- production day, 'YYYY-MM-DD'
  qty_original  INTEGER NOT NULL DEFAULT 0,
  qty_matcha    INTEGER NOT NULL DEFAULT 0,
  qty_hojicha   INTEGER NOT NULL DEFAULT 0,
  qty_other     INTEGER NOT NULL DEFAULT 0,
  deliverer     TEXT,
  note          TEXT,
  delivery_date TEXT,                     -- when it ships; NULL = same as production day
  delivered     INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(date);
-- idx_orders_delivery is created in db.py after the delivery_date migration,
-- so it works on databases created before that column existed.

-- Extra hours not tied to a production shift (pop-ups, markets, etc.)
-- Plus per-entry transportation reimbursement (bridge toll etc.)
CREATE TABLE IF NOT EXISTS popups (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id  INTEGER NOT NULL REFERENCES employees(id),
  date         TEXT    NOT NULL,  -- 'YYYY-MM-DD'
  description  TEXT    NOT NULL DEFAULT '',
  hours        REAL    NOT NULL DEFAULT 0,
  hourly_rate  REAL    NOT NULL DEFAULT 0,  -- $/hr; 0 = use gusto_rate setting
  transport    REAL    NOT NULL DEFAULT 0,  -- transportation reimbursement in $
  created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_popups_date ON popups(date);

-- Employee availability for delivery-only days (no production shift that day)
CREATE TABLE IF NOT EXISTS delivery_availability (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  date        TEXT    NOT NULL,  -- 'YYYY-MM-DD'
  created_at  TEXT    NOT NULL,
  UNIQUE(employee_id, date)
);
CREATE INDEX IF NOT EXISTS idx_del_av_date ON delivery_availability(date);

-- Strawberry purchases per employee per date (deducted from salary at $strawberry_price each)
CREATE TABLE IF NOT EXISTS strawberry_purchases (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  date        TEXT    NOT NULL,  -- 'YYYY-MM-DD'
  quantity    INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strawberry_date ON strawberry_purchases(date);

-- Manager post-shift reports (inventory + memo + actual hours).
CREATE TABLE IF NOT EXISTS shift_reports (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  submitted_by      INTEGER NOT NULL REFERENCES employees(id),
  status            TEXT    NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
  strawberry_stock  INTEGER,
  anko_stock        INTEGER,
  memo              TEXT,
  submitted_at      TEXT    NOT NULL,
  decided_at        TEXT,
  UNIQUE(shift_instance_id)
);

CREATE TABLE IF NOT EXISTS shift_report_hours (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id   INTEGER NOT NULL REFERENCES shift_reports(id),
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  actual_start TEXT   NOT NULL,
  actual_end   TEXT   NOT NULL,
  UNIQUE(report_id, employee_id)
);

-- Default manager per weekday (Mon=0 … Sun=6).
CREATE TABLE IF NOT EXISTS weekday_managers (
  weekday     INTEGER PRIMARY KEY,
  employee_id INTEGER REFERENCES employees(id)
);

-- Dates when no delivery happens (owner can toggle per day).
CREATE TABLE IF NOT EXISTS delivery_blackout (
  date TEXT PRIMARY KEY
);

-- Default quantities per client per weekday, used to pre-fill orders each month.
CREATE TABLE IF NOT EXISTS recurring_orders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id    INTEGER NOT NULL REFERENCES clients(id),
  weekday      INTEGER NOT NULL,  -- 0=Mon … 6=Sun
  qty_original INTEGER NOT NULL DEFAULT 0,
  qty_matcha   INTEGER NOT NULL DEFAULT 0,
  qty_hojicha  INTEGER NOT NULL DEFAULT 0,
  qty_other    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(client_id, weekday)
);
