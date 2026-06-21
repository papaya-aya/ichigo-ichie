-- Ichigo Ichie shift manager — PostgreSQL schema

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  pin_hash   TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- Weekly recurring production shifts. weekday uses Python's convention: Mon=0 .. Sun=6.
CREATE TABLE IF NOT EXISTS shift_templates (
  id         SERIAL PRIMARY KEY,
  weekday    INTEGER NOT NULL,
  label      TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time   TEXT NOT NULL,
  quantity   INTEGER NOT NULL,
  min_people INTEGER NOT NULL,
  max_people INTEGER NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1
);

-- A concrete dated occurrence of a template (e.g. Mon 2026-07-06).
CREATE TABLE IF NOT EXISTS shift_instances (
  id          SERIAL PRIMARY KEY,
  template_id INTEGER NOT NULL REFERENCES shift_templates(id),
  date        TEXT NOT NULL,
  UNIQUE(template_id, date)
);

-- An employee's submitted availability for one shift instance.
CREATE TABLE IF NOT EXISTS availability (
  id                SERIAL PRIMARY KEY,
  employee_id       INTEGER NOT NULL REFERENCES employees(id),
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  start_time        TEXT NOT NULL,
  end_time          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
  note              TEXT,
  submitted_at      TEXT NOT NULL,
  decided_at        TEXT,
  can_deliver       INTEGER NOT NULL DEFAULT 0,
  is_update         INTEGER NOT NULL DEFAULT 0,
  UNIQUE(employee_id, shift_instance_id)
);

-- Who is actually scheduled on a shift instance.
CREATE TABLE IF NOT EXISTS assignments (
  id                SERIAL PRIMARY KEY,
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  employee_id       INTEGER NOT NULL REFERENCES employees(id),
  start_time        TEXT NOT NULL,
  end_time          TEXT NOT NULL,
  is_manager        INTEGER NOT NULL DEFAULT 0,
  actual_start      TEXT,
  actual_end        TEXT,
  strawberries_bought INTEGER NOT NULL DEFAULT 0,
  UNIQUE(shift_instance_id, employee_id)
);

-- Restaurants / cafes we sell to.
CREATE TABLE IF NOT EXISTS clients (
  id     SERIAL PRIMARY KEY,
  name   TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);

-- One client's order for one production date.
CREATE TABLE IF NOT EXISTS orders (
  id            SERIAL PRIMARY KEY,
  client_id     INTEGER NOT NULL REFERENCES clients(id),
  date          TEXT NOT NULL,
  qty_original  INTEGER NOT NULL DEFAULT 0,
  qty_matcha    INTEGER NOT NULL DEFAULT 0,
  qty_hojicha   INTEGER NOT NULL DEFAULT 0,
  qty_other     INTEGER NOT NULL DEFAULT 0,
  deliverer     TEXT,
  note          TEXT,
  delivery_date TEXT,
  delivered     INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(date);
CREATE INDEX IF NOT EXISTS idx_orders_delivery ON orders(delivery_date);

-- Extra hours not tied to a production shift (pop-ups, markets, etc.)
CREATE TABLE IF NOT EXISTS popups (
  id           SERIAL PRIMARY KEY,
  employee_id  INTEGER NOT NULL REFERENCES employees(id),
  date         TEXT    NOT NULL,
  description  TEXT    NOT NULL DEFAULT '',
  hours        REAL    NOT NULL DEFAULT 0,
  hourly_rate  REAL    NOT NULL DEFAULT 0,
  transport    REAL    NOT NULL DEFAULT 0,
  created_at   TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'approved',
  requested_by INTEGER,
  start_time   TEXT,
  end_time     TEXT
);
CREATE INDEX IF NOT EXISTS idx_popups_date ON popups(date);

-- Employee availability for delivery-only days.
CREATE TABLE IF NOT EXISTS delivery_availability (
  id          SERIAL PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  date        TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  UNIQUE(employee_id, date)
);
CREATE INDEX IF NOT EXISTS idx_del_av_date ON delivery_availability(date);

-- Strawberry purchases per employee per date.
CREATE TABLE IF NOT EXISTS strawberry_purchases (
  id          SERIAL PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  date        TEXT    NOT NULL,
  quantity    INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL,
  status      TEXT    NOT NULL DEFAULT 'approved',
  requested_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_strawberry_date ON strawberry_purchases(date);

-- Manager post-shift reports.
CREATE TABLE IF NOT EXISTS shift_reports (
  id                SERIAL PRIMARY KEY,
  shift_instance_id INTEGER NOT NULL REFERENCES shift_instances(id),
  submitted_by      INTEGER NOT NULL REFERENCES employees(id),
  status            TEXT    NOT NULL DEFAULT 'pending',
  strawberry_stock  INTEGER,
  anko_stock        INTEGER,
  memo              TEXT,
  submitted_at      TEXT    NOT NULL,
  decided_at        TEXT,
  UNIQUE(shift_instance_id)
);

CREATE TABLE IF NOT EXISTS shift_report_hours (
  id           SERIAL PRIMARY KEY,
  report_id    INTEGER NOT NULL REFERENCES shift_reports(id),
  employee_id  INTEGER NOT NULL REFERENCES employees(id),
  actual_start TEXT    NOT NULL,
  actual_end   TEXT    NOT NULL,
  UNIQUE(report_id, employee_id)
);

-- Default manager per weekday.
CREATE TABLE IF NOT EXISTS weekday_managers (
  weekday     INTEGER PRIMARY KEY,
  employee_id INTEGER REFERENCES employees(id)
);

-- Dates when no delivery happens.
CREATE TABLE IF NOT EXISTS delivery_blackout (
  date TEXT PRIMARY KEY
);

-- Default quantities per client per weekday.
CREATE TABLE IF NOT EXISTS recurring_orders (
  id           SERIAL PRIMARY KEY,
  client_id    INTEGER NOT NULL REFERENCES clients(id),
  weekday      INTEGER NOT NULL,
  qty_original INTEGER NOT NULL DEFAULT 0,
  qty_matcha   INTEGER NOT NULL DEFAULT 0,
  qty_hojicha  INTEGER NOT NULL DEFAULT 0,
  qty_other    INTEGER NOT NULL DEFAULT 0,
  delivery_offset INTEGER NOT NULL DEFAULT 0,
  UNIQUE(client_id, weekday)
);
