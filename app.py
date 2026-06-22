"""Ichigo Ichie — shift management web app (Flask + SQLite)."""
import calendar as _calendar
import os
import urllib.request
import json as _json
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask, flash, g, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash

import db as database
from db import hash_password as generate_password_hash, FLAVORS
from scheduler import (
    auto_assign, coverage, generate_instances, to_minutes, to_hhmm,
)
import production

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SHIFTO_SECRET", "dev-secret-change-me")

# Initialize DB on startup (works for both local and serverless/Vercel).
database.init_db()

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

@app.before_request
def open_db():
    g.db = database.get_db()


@app.teardown_request
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_globals():
    return {
        "is_owner": session.get("owner", False),
        "employee_name": session.get("employee_name"),
        "today": date.today(),
        "enumerate": enumerate,
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_login(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("owner") and not session.get("employee_id"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def require_owner(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("owner"):
            flash("Owner access only.", "error")
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


# ---------------------------------------------------------------------------
# Month helpers
# ---------------------------------------------------------------------------

def next_month_str(today=None):
    today = today or date.today()
    y, m = today.year, today.month
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def parse_month(s):
    y, m = s.split("-")
    return int(y), int(m)


def shift_month(month_str, delta):
    y, m = parse_month(month_str)
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y}-{m:02d}"


def month_label(month_str):
    y, m = parse_month(month_str)
    return f"{_calendar.month_name[m]} {y}"


def instances_for_month(month_str):
    """Shift instances in the month, joined with template info, date-ordered."""
    return g.db.execute(
        """SELECT si.id AS instance_id, si.date, t.*
             FROM shift_instances si
             JOIN shift_templates t ON t.id = si.template_id
            WHERE si.date LIKE ?
            ORDER BY si.date, t.start_time""",
        (month_str + "-%",),
    ).fetchall()


def strawberry_price():
    try:
        return float(database.get_setting(g.db, "strawberry_price", "10.00"))
    except (TypeError, ValueError):
        return 10.0

def delivery_transport_amount():
    try:
        return float(database.get_setting(g.db, "delivery_transport", "6.00"))
    except (TypeError, ValueError):
        return 6.0

STRAWBERRY_PRICE = 10.0  # fallback constant; runtime uses strawberry_price()

def assigned_people(instance_id):
    return g.db.execute(
        """SELECT a.employee_id, e.name, a.start_time AS start, a.end_time AS end,
                  a.is_manager, a.actual_start, a.actual_end, a.strawberries_bought
             FROM assignments a JOIN employees e ON e.id = a.employee_id
            WHERE a.shift_instance_id = ?
            ORDER BY e.name""",
        (instance_id,),
    ).fetchall()


def weekday_manager_id(weekday: int):
    """Return the configured default manager employee_id for this weekday, or None."""
    row = g.db.execute(
        "SELECT employee_id FROM weekday_managers WHERE weekday=?", (weekday,)
    ).fetchone()
    return row["employee_id"] if row else None


def target_productivity():
    try:
        return float(database.get_setting(g.db, "target_productivity", "6.5"))
    except (TypeError, ValueError):
        return 6.5


def piece_rate():
    try:
        return float(database.get_setting(g.db, "piece_rate", "2.00"))
    except (TypeError, ValueError):
        return 2.0


def gusto_rate():
    try:
        return float(database.get_setting(g.db, "gusto_rate", "20.00"))
    except (TypeError, ValueError):
        return 20.0


def approved_candidates(instance_id):
    return [
        {
            "employee_id": r["employee_id"],
            "name":        r["name"],
            "start":       r["start"],
            "end":         r["end"],
            "if_needed":   bool(r["if_needed"]),
        }
        for r in g.db.execute(
            """SELECT av.employee_id, e.name, av.start_time AS start, av.end_time AS end,
                      av.if_needed
                 FROM availability av JOIN employees e ON e.id = av.employee_id
                WHERE av.shift_instance_id = ? AND av.status = 'approved'
                ORDER BY av.if_needed, e.name""",
            (instance_id,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    if session.get("owner"):
        return redirect(url_for("owner_dashboard"))
    if session.get("employee_id"):
        return redirect(url_for("availability"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    employees = g.db.execute(
        "SELECT id, name FROM employees WHERE active = 1 ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        role = request.form.get("role")
        if role == "owner":
            stored = database.get_setting(g.db, "owner_password_hash")
            if stored and check_password_hash(stored, request.form.get("password", "")):
                session.clear()
                session["owner"] = True
                return redirect(url_for("owner_dashboard"))
            flash("Wrong owner password.", "error")
        else:
            emp = g.db.execute(
                "SELECT * FROM employees WHERE id = ? AND active = 1",
                (request.form.get("employee_id"),),
            ).fetchone()
            if emp and emp["pin_hash"] == "":
                # No PIN set yet — send to registration
                session["register_emp_id"] = emp["id"]
                return redirect(url_for("register_pin"))
            if emp and check_password_hash(emp["pin_hash"], request.form.get("pin", "")):
                session.clear()
                session["employee_id"] = emp["id"]
                session["employee_name"] = emp["name"]
                return redirect(url_for("availability"))
            flash("Wrong name or PIN.", "error")

    return render_template("login.html", employees=employees)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/register-pin", methods=["GET", "POST"])
def register_pin():
    emp_id = session.get("register_emp_id")
    if not emp_id:
        return redirect(url_for("login"))
    emp = g.db.execute(
        "SELECT * FROM employees WHERE id = ? AND active = 1 AND pin_hash = ''",
        (emp_id,),
    ).fetchone()
    if not emp:
        session.pop("register_emp_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        confirm = request.form.get("confirm", "").strip()
        if len(pin) < 4:
            flash("PIN must be at least 4 characters.", "error")
        elif pin != confirm:
            flash("PINs do not match.", "error")
        else:
            g.db.execute(
                "UPDATE employees SET pin_hash = ? WHERE id = ?",
                (generate_password_hash(pin), emp_id),
            )
            g.db.commit()
            session.pop("register_emp_id", None)
            session["employee_id"] = emp["id"]
            session["employee_name"] = emp["name"]
            flash(f"Welcome, {emp['name']}! Your PIN is set.", "success")
            return redirect(url_for("availability"))

    return render_template("register_pin.html", emp_name=emp["name"])


# ---------------------------------------------------------------------------
# Employee — availability submission
# ---------------------------------------------------------------------------

@app.route("/availability", methods=["GET", "POST"])
@require_login
def availability():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))

    month = request.values.get("month") or next_month_str()

    # Delivery-only dates: dates in this month that have orders but no production shift
    def _delivery_only_dates(month_str):
        production_dates = {
            r["date"] for r in g.db.execute(
                "SELECT date FROM shift_instances WHERE date LIKE ?", (month_str + "-%",)
            ).fetchall()
        }
        all_del = g.db.execute(
            """SELECT DISTINCT COALESCE(delivery_date, date) AS d
                 FROM orders WHERE COALESCE(delivery_date, date) LIKE ?
                 ORDER BY d""",
            (month_str + "-%",),
        ).fetchall()
        return [
            r["d"] for r in all_del
            if r["d"] not in production_dates
            and date.fromisoformat(r["d"]).weekday() != 5  # exclude Saturday
        ]

    if request.method == "POST":
        instances = instances_for_month(month)
        saved, errors = 0, []
        for inst in instances:
            iid = inst["instance_id"]
            work_checked     = bool(request.form.get(f"work_{iid}"))
            if_needed_checked = bool(request.form.get(f"if_needed_{iid}"))
            if work_checked or if_needed_checked:
                start = request.form.get(f"start_{iid}") or inst["start_time"]
                end = request.form.get(f"end_{iid}") or inst["end_time"]
                if not _within(start, end, inst):
                    errors.append(f"{inst['date']} {inst['label']}: time must be within "
                                  f"{inst['start_time']}–{inst['end_time']} and start before end.")
                    continue
                can_deliver = 1 if request.form.get(f"deliver_{iid}") else 0
                if_needed = 1 if if_needed_checked else 0
                g.db.execute(
                    """INSERT INTO availability
                         (employee_id, shift_instance_id, start_time, end_time,
                          status, note, can_deliver, if_needed, submitted_at, is_update)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0)
                       ON CONFLICT(employee_id, shift_instance_id) DO UPDATE SET
                         start_time   = excluded.start_time,
                         end_time     = excluded.end_time,
                         status       = 'pending',
                         note         = excluded.note,
                         can_deliver  = excluded.can_deliver,
                         if_needed    = excluded.if_needed,
                         submitted_at = excluded.submitted_at,
                         decided_at   = NULL,
                         is_update    = 1""",
                    (emp_id, iid, start, end,
                     request.form.get(f"note_{iid}", "").strip(),
                     can_deliver, if_needed, database.now_iso()),
                )
                saved += 1
            else:
                g.db.execute(
                    "DELETE FROM availability WHERE employee_id = ? AND shift_instance_id = ?",
                    (emp_id, iid),
                )

        # Save delivery-only day availability
        del_saved = 0
        for d in _delivery_only_dates(month):
            if request.form.get(f"del_day_{d}"):
                g.db.execute(
                    """INSERT INTO delivery_availability (employee_id, date, created_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(employee_id, date) DO NOTHING""",
                    (emp_id, d, database.now_iso()),
                )
                del_saved += 1
            else:
                g.db.execute(
                    "DELETE FROM delivery_availability WHERE employee_id=? AND date=?",
                    (emp_id, d),
                )
        try:
            g.db.commit()
        except Exception as db_err:
            import traceback
            app.logger.error("Availability commit failed: %s\n%s", db_err, traceback.format_exc())
            flash(f"Database error: {db_err}", "error")
            return redirect(url_for("availability", month=month))
        for e in errors:
            flash(e, "error")
        if not errors:
            parts = []
            if saved:     parts.append(f"{saved} shift(s)")
            if del_saved: parts.append(f"{del_saved} delivery day(s)")
            flash(f"Saved: {', '.join(parts) or 'no changes'}. Shift submissions await owner approval.", "success")
        return redirect(url_for("availability", month=month))

    instances = instances_for_month(month)
    existing = {
        r["shift_instance_id"]: r
        for r in g.db.execute(
            """SELECT av.* FROM availability av
                JOIN shift_instances si ON si.id = av.shift_instance_id
               WHERE av.employee_id = ? AND si.date LIKE ?""",
            (emp_id, month + "-%"),
        ).fetchall()
    }
    rows = _decorate_instances(instances, existing)

    # Delivery-only dates and which ones this employee already marked
    del_dates = _delivery_only_dates(month)
    del_marked = {
        r["date"] for r in g.db.execute(
            "SELECT date FROM delivery_availability WHERE employee_id=? AND date LIKE ?",
            (emp_id, month + "-%"),
        ).fetchall()
    }
    del_rows = []
    for d in del_dates:
        y, mo, dy = (int(x) for x in d.split("-"))
        del_rows.append({
            "date":    d,
            "weekday": WEEKDAY_NAMES[date(y, mo, dy).weekday()],
            "checked": d in del_marked,
        })

    return render_template(
        "availability.html",
        month=month, month_label=month_label(month),
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        rows=rows, del_rows=del_rows,
    )


@app.route("/my-shifts")
@require_login
def my_shifts():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))

    today_date = date.today()
    pr = piece_rate()
    gr = gusto_rate()

    # --- Upcoming confirmed shifts (assigned, today onwards) ---
    upcoming_rows = g.db.execute(
        """SELECT a.start_time, a.end_time, a.actual_start, a.actual_end, a.is_manager,
                  si.id AS instance_id, si.date, t.label, t.weekday,
                  t.start_time AS shift_start, t.end_time AS shift_end,
                  sr.status AS report_status
             FROM assignments a
             JOIN shift_instances si ON si.id = a.shift_instance_id
             JOIN shift_templates t  ON t.id = si.template_id
             LEFT JOIN shift_reports sr ON sr.shift_instance_id = si.id
            WHERE a.employee_id = ? AND si.date >= ?
            ORDER BY si.date""",
        (emp_id, today_date.isoformat()),
    ).fetchall()
    upcoming = []
    for r in upcoming_rows:
        window = (r["actual_start"] or r["start_time"]) + "–" + (r["actual_end"] or r["end_time"])
        upcoming.append({
            "date":          r["date"],
            "weekday":       WEEKDAY_NAMES[r["weekday"]],
            "label":         r["label"],
            "shift_hours":   f"{r['shift_start']}–{r['shift_end']}",
            "window":        window,
            "is_manager":    bool(r["is_manager"]),
            "confirmed":     bool(r["actual_start"]),
            "instance_id":   r["instance_id"],
            "report_status": r["report_status"],
        })

    # --- Pay period ---
    date_from = request.values.get("from") or today_date.replace(day=1).isoformat()
    date_to   = request.values.get("to")   or today_date.isoformat()

    # Shifts in the pay period
    pay_rows = g.db.execute(
        """SELECT COALESCE(a.actual_start, a.start_time) AS a_start,
                  COALESCE(a.actual_end,   a.end_time)   AS a_end,
                  a.actual_start, a.is_manager,
                  si.id AS instance_id, si.date, t.weekday, t.label
             FROM assignments a
             JOIN shift_instances si ON si.id = a.shift_instance_id
             JOIN shift_templates t  ON t.id = si.template_id
            WHERE a.employee_id = ? AND si.date BETWEEN ? AND ?
            ORDER BY si.date""",
        (emp_id, date_from, date_to),
    ).fetchall()

    shifts_pay = []
    total_hours = 0.0
    total_pay   = 0.0

    for r in pay_rows:
        # total person-hours across the whole shift (for rate calc)
        all_assigned = g.db.execute(
            """SELECT COALESCE(actual_start, start_time) AS s,
                      COALESCE(actual_end,   end_time)   AS e
                 FROM assignments WHERE shift_instance_id = ?""",
            (r["instance_id"],),
        ).fetchall()
        total_ph = sum((to_minutes(a["e"]) - to_minutes(a["s"])) / 60.0 for a in all_assigned)
        pieces = production.day_totals(g.db, r["date"])["total"]
        rate   = (pr * pieces / total_ph) if (total_ph > 0 and pieces > 0) else 0.0
        hrs    = (to_minutes(r["a_end"]) - to_minutes(r["a_start"])) / 60.0
        pay    = hrs * rate * (1.05 if r["is_manager"] else 1.0)
        shifts_pay.append({
            "date":        r["date"],
            "weekday":     WEEKDAY_NAMES[r["weekday"]],
            "label":       r["label"],
            "time_window": f"{r['a_start']}–{r['a_end']}",
            "confirmed":   bool(r["actual_start"]),
            "hours":       round(hrs, 2),
            "pieces":      pieces,
            "rate":        round(rate, 2),
            "pay":         round(pay, 2),
            "is_manager":  bool(r["is_manager"]),
        })
        total_hours += hrs
        total_pay   += pay

    # Pop-up entries
    popup_rows = g.db.execute(
        "SELECT * FROM popups WHERE employee_id = ? AND date BETWEEN ? AND ? ORDER BY date",
        (emp_id, date_from, date_to),
    ).fetchall()
    popups_pay = []
    for p in popup_rows:
        r    = p["hourly_rate"] if p["hourly_rate"] else gr
        pay  = round(p["hours"] * r, 2)
        popups_pay.append({
            "date":        p["date"],
            "description": p["description"] or "Pop-up",
            "time":        f"{p['start_time']}–{p['end_time']}" if p["start_time"] else "—",
            "hours":       round(p["hours"], 2),
            "rate":        r,
            "pay":         pay,
            "transport":   p["transport"],
        })
        total_hours += p["hours"]
        total_pay   += pay

    # Strawberry deductions
    sp_rows = g.db.execute(
        "SELECT * FROM strawberry_purchases WHERE employee_id = ? AND date BETWEEN ? AND ? ORDER BY date",
        (emp_id, date_from, date_to),
    ).fetchall()
    strawberries = []
    total_strawberry = 0.0
    for sp in sp_rows:
        cost = sp["quantity"] * STRAWBERRY_PRICE
        strawberries.append({"date": sp["date"], "quantity": sp["quantity"], "cost": round(cost, 2)})
        total_strawberry += cost

    # Delivery transport (Wed=2 / Thu=3) — base + per stop
    dt_amt   = delivery_transport_amount()
    emp_name = g.db.execute("SELECT name FROM employees WHERE id=?", (emp_id,)).fetchone()["name"]
    dt_rows  = g.db.execute(
        """SELECT COALESCE(delivery_date, date) AS deliver_on, COUNT(*) AS stops
             FROM orders
            WHERE COALESCE(delivery_date, date) BETWEEN ? AND ?
              AND lower(deliverer) = lower(?)
            GROUP BY COALESCE(delivery_date, date)""",
        (date_from, date_to, emp_name),
    ).fetchall()
    transports = []
    total_transport = 0.0
    for dr in dt_rows:
        d_str = dr["deliver_on"]
        y, mo, dy = (int(x) for x in d_str.split("-"))
        wday = date(y, mo, dy).weekday()
        if wday not in (0, 1, 2, 3, 4):
            continue
        stops    = dr["stops"]
        has_base = wday in (2, 3)
        amount   = round((dt_amt + dt_amt * stops) if has_base else (dt_amt * stops), 2)
        transports.append({
            "date": d_str, "weekday": WEEKDAY_NAMES[wday],
            "stops": stops, "has_base": has_base, "amount": amount,
        })
        total_transport += amount

    total_hours      = round(total_hours, 2)
    total_pay        = round(total_pay, 2)
    total_strawberry = round(total_strawberry, 2)
    total_transport  = round(total_transport, 2)
    net_pay          = round(max(total_pay + total_transport + total_strawberry, 0.0), 2)
    gusto_hours      = round(net_pay / gr, 2) if gr else 0.0

    sp_price = strawberry_price()

    # Also filter salary by approved only
    strawberries = [s for s in strawberries]  # already filtered above
    popups_pay   = [p for p in popups_pay]

    # Pending requests for this employee
    pending_sp = g.db.execute(
        "SELECT id, date, created_at FROM strawberry_purchases WHERE employee_id=? AND status='pending' ORDER BY date",
        (emp_id,),
    ).fetchall()
    pending_pp = g.db.execute(
        "SELECT id, date, description, start_time, end_time, hours, transport FROM popups WHERE employee_id=? AND status='pending' ORDER BY date",
        (emp_id,),
    ).fetchall()

    return render_template(
        "my_shifts.html",
        upcoming=upcoming,
        date_from=date_from, date_to=date_to,
        shifts=shifts_pay, popups=popups_pay,
        strawberries=strawberries, transports=transports,
        total_hours=total_hours, total_pay=total_pay,
        total_strawberry=total_strawberry, total_transport=total_transport,
        net_pay=net_pay, gusto_hours=gusto_hours,
        piece_rate=pr, gusto_rate=gr,
        strawberry_price=sp_price,
        pending_sp=pending_sp, pending_pp=pending_pp,
    )


def _within(start, end, inst):
    try:
        s, e = to_minutes(start), to_minutes(end)
    except (ValueError, AttributeError):
        return False
    return (to_minutes(inst["start_time"]) <= s < e <= to_minutes(inst["end_time"]))


def _decorate_instances(instances, existing):
    rows = []
    for inst in instances:
        av = existing.get(inst["instance_id"])
        rows.append({
            "inst":        inst,
            "weekday":     WEEKDAY_NAMES[inst["weekday"]],
            "checked":     av is not None and not av["if_needed"],
            "start":       av["start_time"]  if av else inst["start_time"],
            "end":         av["end_time"]    if av else inst["end_time"],
            "note":        av["note"]        if av else "",
            "status":      av["status"]      if av else None,
            "can_deliver": bool(av["can_deliver"]) if av else False,
            "if_needed":   bool(av["if_needed"])   if av else False,
        })
    return rows


# ---------------------------------------------------------------------------
# Shared calendar
# ---------------------------------------------------------------------------

@app.route("/calendar")
@require_login
def calendar_view():
    month = request.values.get("month") or date.today().strftime("%Y-%m")
    today_str = date.today().isoformat()
    current_emp_id = session.get("employee_id")

    # --- production shifts ---
    instances = instances_for_month(month)
    shifts_by_date = {}
    for inst in instances:
        people  = assigned_people(inst["instance_id"])
        day_ord = production.orders_for_date(g.db, inst["date"])
        orders  = [{**dict(o), "total": production.order_total(o)} for o in day_ord]
        shifts_by_date[inst["date"]] = {
            "inst":    inst,
            "weekday": WEEKDAY_NAMES[inst["weekday"]],
            "people":  [dict(p) for p in people],
            "orders":  orders,
            "pieces":  sum(o["total"] for o in orders),
        }

    # --- delivery info grouped by delivery date, then by deliverer ---
    delivery_rows = g.db.execute(
        """SELECT COALESCE(o.delivery_date, o.date) AS deliver_on,
                  o.client_id, c.name AS client_name,
                  SUM(o.qty_original) AS qty_original,
                  SUM(o.qty_matcha)   AS qty_matcha,
                  SUM(o.qty_hojicha)  AS qty_hojicha,
                  SUM(o.qty_other)    AS qty_other,
                  SUM(o.qty_original+o.qty_matcha+o.qty_hojicha+o.qty_other) AS total,
                  MAX(o.delivered)    AS delivered,
                  MAX(o.deliverer)    AS deliverer
             FROM orders o JOIN clients c ON c.id = o.client_id
            WHERE COALESCE(o.delivery_date, o.date) LIKE ?
            GROUP BY deliver_on, o.client_id, c.name
            ORDER BY deliver_on, c.name""",
        (month + "-%",),
    ).fetchall()

    deliveries_by_date = {}
    for r in delivery_rows:
        d = r["deliver_on"]
        if d not in deliveries_by_date:
            deliveries_by_date[d] = {"by_deliverer": {}, "pcs": 0, "n_done": 0, "n_total": 0}
        info = deliveries_by_date[d]
        deliverer_key = r["deliverer"] or ""
        info["by_deliverer"].setdefault(deliverer_key, []).append(dict(r))
        info["pcs"]     += r["total"] or 0
        info["n_total"] += 1
        info["n_done"]  += 1 if r["delivered"] else 0

    # --- union of all dates with anything to show ---
    all_dates = sorted(set(list(shifts_by_date) + list(deliveries_by_date)))

    days = []
    for d in all_dates:
        y, mo, dy = (int(x) for x in d.split("-"))
        wday = WEEKDAY_NAMES[date(y, mo, dy).weekday()]
        days.append({
            "date":     d,
            "weekday":  shifts_by_date[d]["weekday"] if d in shifts_by_date else wday,
            "is_past":  d < today_str,
            "is_today": d == today_str,
            "shift":    shifts_by_date.get(d),
            "delivery": deliveries_by_date.get(d),
        })

    return render_template(
        "calendar.html",
        month=month, month_label=month_label(month),
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        days=days, flavors=FLAVORS,
        current_emp_id=current_emp_id,
    )


# ---------------------------------------------------------------------------
# Owner — dashboard, employees, month generation, password
# ---------------------------------------------------------------------------

@app.route("/owner")
@require_owner
def owner_dashboard():
    employees = g.db.execute(
        "SELECT id, name, active FROM employees ORDER BY active DESC, name"
    ).fetchall()
    templates = g.db.execute(
        "SELECT * FROM shift_templates WHERE active = 1 ORDER BY weekday"
    ).fetchall()
    pending = g.db.execute(
        "SELECT COUNT(*) AS n FROM availability WHERE status = 'pending'"
    ).fetchone()["n"]
    clients = g.db.execute(
        "SELECT id, name, active FROM clients ORDER BY active DESC, name"
    ).fetchall()

    # Upcoming shifts — month-navigable list
    dash_month = request.values.get("month") or date.today().strftime("%Y-%m")
    today_str  = date.today().isoformat()
    upcoming_instances = instances_for_month(dash_month)

    upcoming = []
    for inst in upcoming_instances:
        people  = assigned_people(inst["instance_id"])
        day_ord = production.orders_for_date(g.db, inst["date"])
        orders  = [{**dict(o), "total": production.order_total(o)} for o in day_ord]
        totals  = production.day_totals(g.db, inst["date"])
        upcoming.append({
            "inst":         inst,
            "weekday":      WEEKDAY_NAMES[inst["weekday"]],
            "people":       [dict(p) for p in people],
            "staff":        [p["name"] for p in people],
            "n_assigned":   len(people),
            "orders":       orders,
            "flavors":      totals["by_flavor"],
            "total_pieces": totals["total"],
            "is_past":      inst["date"] < today_str,
            "is_today":     inst["date"] == today_str,
        })

    weekday_managers = {
        r["weekday"]: r["employee_id"]
        for r in g.db.execute("SELECT weekday, employee_id FROM weekday_managers").fetchall()
    }
    return render_template(
        "owner_dashboard.html",
        employees=employees, templates=templates, pending=pending,
        weekday_names=WEEKDAY_NAMES, next_month=next_month_str(),
        clients=clients, target_productivity=target_productivity(),
        upcoming=upcoming, flavors=database.FLAVORS,
        piece_rate=piece_rate(), gusto_rate=gusto_rate(),
        strawberry_price_val=strawberry_price(),
        delivery_transport_val=delivery_transport_amount(),
        dash_month=dash_month,
        dash_prev=shift_month(dash_month, -1),
        dash_next=shift_month(dash_month, 1),
        dash_label=month_label(dash_month),
        weekday_managers=weekday_managers,
    )


@app.route("/owner/employees/add", methods=["POST"])
@require_owner
def add_employee():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name is required.", "error")
    else:
        try:
            g.db.execute(
                "INSERT INTO employees (name, pin_hash, created_at) VALUES (?, '', ?)",
                (name, database.now_iso()),
            )
            g.db.commit()
            flash(f"Added {name}. They will set their own PIN on first login.", "success")
        except Exception:
            flash(f"An employee named {name} already exists.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/employees/<int:emp_id>/update", methods=["POST"])
@require_owner
def update_employee(emp_id):
    action = request.form.get("action")
    if action == "toggle":
        g.db.execute(
            "UPDATE employees SET active = 1 - active WHERE id = ?", (emp_id,))
        flash("Employee status updated.", "success")
    elif action == "reset_pin":
        g.db.execute("UPDATE employees SET pin_hash = '' WHERE id = ?", (emp_id,))
        flash("PIN cleared — employee will set a new one on next login.", "success")
    g.db.commit()
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/generate", methods=["POST"])
@require_owner
def generate_month():
    month = request.form.get("month") or next_month_str()
    y, m = parse_month(month)
    created = generate_instances(g.db, y, m)
    flash(f"{month_label(month)}: {created} new shift date(s) added.", "success")
    return redirect(url_for("owner_schedule", month=month))


@app.route("/owner/password", methods=["POST"])
@require_owner
def change_password():
    new = request.form.get("new_password", "")
    if len(new) < 6:
        flash("New password must be at least 6 characters.", "error")
    else:
        database.set_setting(g.db, "owner_password_hash", generate_password_hash(new))
        g.db.commit()
        flash("Owner password changed.", "success")
    return redirect(url_for("owner_dashboard"))


# ---------------------------------------------------------------------------
# Owner — production settings, clients, orders
# ---------------------------------------------------------------------------

@app.route("/owner/shift-templates/<int:template_id>/update", methods=["POST"])
@require_owner
def update_shift_template(template_id):
    try:
        weekday    = int(request.form.get("weekday", "0"))
        label      = request.form.get("label", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time   = request.form.get("end_time",   "").strip()
        quantity   = _int(request.form.get("quantity",   "0"))
        min_people = max(1, _int(request.form.get("min_people", "1")))
        max_people = max(1, _int(request.form.get("max_people", "1")))
        if max_people < min_people:
            max_people = min_people
        # basic time format validation
        to_minutes(start_time); to_minutes(end_time)
        g.db.execute(
            """UPDATE shift_templates
                  SET weekday=?, label=?, start_time=?, end_time=?,
                      quantity=?, min_people=?, max_people=?
                WHERE id=?""",
            (weekday, label, start_time, end_time,
             quantity, min_people, max_people, template_id),
        )
        g.db.commit()
        flash(f"Shift template updated.", "success")
    except Exception as e:
        flash(f"Could not update template: {e}", "error")
    return redirect(url_for("owner_dashboard"))


# ---------------------------------------------------------------------------
# Recurring orders
# ---------------------------------------------------------------------------

@app.route("/owner/settings/weekday-managers", methods=["POST"])
@require_owner
def set_weekday_managers():
    for wd in range(5):  # Mon–Fri only
        emp_id = request.form.get(f"mgr_{wd}", "").strip()
        if emp_id:
            g.db.execute(
                "INSERT INTO weekday_managers (weekday, employee_id) VALUES (?, ?)"
                " ON CONFLICT(weekday) DO UPDATE SET employee_id=excluded.employee_id",
                (wd, int(emp_id)),
            )
        else:
            g.db.execute("DELETE FROM weekday_managers WHERE weekday=?", (wd,))
    g.db.commit()
    flash("Weekly managers saved.", "success")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/settings/strawberry-price", methods=["POST"])
@require_owner
def set_strawberry_price():
    try:
        val = float(request.form.get("strawberry_price", ""))
        if val < 0:
            raise ValueError
        database.set_setting(g.db, "strawberry_price", f"{val:.2f}")
        g.db.commit()
        flash(f"Strawberry price set to ${val:.2f}.", "success")
    except ValueError:
        flash("Invalid value.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/settings/delivery-transport", methods=["POST"])
@require_owner
def set_delivery_transport():
    try:
        val = float(request.form.get("delivery_transport", ""))
        if val < 0:
            raise ValueError
        database.set_setting(g.db, "delivery_transport", f"{val:.2f}")
        g.db.commit()
        flash(f"Delivery transport set to ${val:.2f}.", "success")
    except ValueError:
        flash("Invalid value.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/recurring-orders")
@require_owner
def recurring_orders():
    templates = g.db.execute(
        "SELECT * FROM shift_templates WHERE active=1 ORDER BY weekday"
    ).fetchall()
    clients = g.db.execute(
        "SELECT * FROM clients WHERE active=1 ORDER BY name"
    ).fetchall()
    # Build a lookup: (client_id, weekday) -> recurring_order row
    rows = g.db.execute("SELECT * FROM recurring_orders").fetchall()
    lookup = {(r["client_id"], r["weekday"]): dict(r) for r in rows}
    import math as _math
    tp = target_productivity()
    # Per-weekday totals from recurring orders (for sync preview)
    wday_totals = {}
    for r in rows:
        qty = r["qty_original"] + r["qty_matcha"] + r["qty_hojicha"] + r["qty_other"]
        wday_totals[r["weekday"]] = wday_totals.get(r["weekday"], 0) + qty
    sync_preview = []
    for t in templates:
        qty = wday_totals.get(t["weekday"], 0)
        shift_hrs = (to_minutes(t["end_time"]) - to_minutes(t["start_time"])) / 60.0
        if qty > 0 and tp and shift_hrs:
            raw     = _math.ceil((qty / tp) / shift_hrs)
            new_min = max(1, raw)
            new_max = max(new_min, raw + 1)
        else:
            raw = new_min = new_max = None
        sync_preview.append({
            "label":    t["label"],
            "weekday":  t["weekday"],
            "cur_qty":  t["quantity"],
            "new_qty":  qty,
            "new_min":  new_min,
            "new_max":  new_max,
        })
    return render_template(
        "recurring_orders.html",
        templates=templates, clients=clients,
        lookup=lookup, weekday_names=WEEKDAY_NAMES,
        flavors=FLAVORS, next_month=next_month_str(),
        sync_preview=sync_preview,
    )


@app.route("/owner/recurring-orders/add-client", methods=["POST"])
@require_owner
def recurring_add_client():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Client name required.", "error")
    else:
        try:
            g.db.execute("INSERT INTO clients (name) VALUES (?)", (name,))
            g.db.commit()
            flash(f"Added client {name}.", "success")
        except Exception:
            flash(f"Client '{name}' already exists.", "error")
    return redirect(url_for("recurring_orders"))


@app.route("/owner/recurring-orders/save", methods=["POST"])
@require_owner
def save_recurring_order():
    client_id       = _int(request.form.get("client_id", "0"))
    weekday         = _int(request.form.get("weekday",   "0"))
    delivery_offset = _int(request.form.get("delivery_offset", "0"))
    qtys = [_int(request.form.get(f"qty_{s}")) for s, _ in FLAVORS]
    if not client_id:
        flash("Client required.", "error")
        return redirect(url_for("recurring_orders"))
    g.db.execute(
        """INSERT INTO recurring_orders
             (client_id, weekday, qty_original, qty_matcha, qty_hojicha, qty_other, delivery_offset)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client_id, weekday) DO UPDATE SET
             qty_original    = excluded.qty_original,
             qty_matcha      = excluded.qty_matcha,
             qty_hojicha     = excluded.qty_hojicha,
             qty_other       = excluded.qty_other,
             delivery_offset = excluded.delivery_offset""",
        (client_id, weekday, *qtys, delivery_offset),
    )
    g.db.commit()
    flash("Recurring order saved.", "success")
    return redirect(url_for("recurring_orders"))


@app.route("/owner/recurring-orders/delete", methods=["POST"])
@require_owner
def delete_recurring_order():
    client_id = _int(request.form.get("client_id", "0"))
    weekday   = _int(request.form.get("weekday",   "0"))
    g.db.execute(
        "DELETE FROM recurring_orders WHERE client_id=? AND weekday=?",
        (client_id, weekday),
    )
    g.db.commit()
    flash("Recurring order removed.", "success")
    return redirect(url_for("recurring_orders"))


@app.route("/owner/recurring-orders/apply", methods=["POST"])
@require_owner
def apply_recurring_orders():
    from datetime import timedelta
    from collections import defaultdict
    month = request.form.get("month") or next_month_str()
    instances = instances_for_month(month)
    rec_rows = g.db.execute(
        "SELECT ro.*, c.name AS client_name FROM recurring_orders ro JOIN clients c ON c.id=ro.client_id"
    ).fetchall()
    by_weekday = defaultdict(list)
    for r in rec_rows:
        by_weekday[r["weekday"]].append(r)

    added = updated = 0
    for inst in instances:
        wday = inst["weekday"]
        prod_date = date(*[int(x) for x in inst["date"].split("-")])
        for rec in by_weekday.get(wday, []):
            offset   = rec["delivery_offset"] or 0
            del_date = (prod_date + timedelta(days=offset)).isoformat()

            existing = g.db.execute(
                "SELECT id, delivery_date FROM orders WHERE client_id=? AND date=?",
                (rec["client_id"], inst["date"]),
            ).fetchone()

            if existing:
                # Always sync quantities to match the recurring config.
                # Only fix delivery_date if it's still sitting at the production date
                # (meaning the offset was never applied); leave manual overrides alone.
                current_del = existing["delivery_date"] or inst["date"]
                fixed_del   = del_date if current_del == inst["date"] else current_del
                g.db.execute(
                    """UPDATE orders
                          SET qty_original=?, qty_matcha=?, qty_hojicha=?, qty_other=?,
                              delivery_date=?
                        WHERE id=?""",
                    (rec["qty_original"], rec["qty_matcha"],
                     rec["qty_hojicha"], rec["qty_other"],
                     fixed_del, existing["id"]),
                )
                updated += 1
            else:
                g.db.execute(
                    """INSERT INTO orders
                         (client_id, date, qty_original, qty_matcha, qty_hojicha, qty_other,
                          delivery_date, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rec["client_id"], inst["date"],
                     rec["qty_original"], rec["qty_matcha"],
                     rec["qty_hojicha"], rec["qty_other"],
                     del_date, database.now_iso()),
                )
                added += 1
    g.db.commit()
    parts = []
    if added:   parts.append(f"{added} new order(s) added")
    if updated: parts.append(f"{updated} delivery date(s) corrected")
    flash(f"{month_label(month)}: {', '.join(parts) or 'nothing to change'}.", "success")
    return redirect(url_for("recurring_orders"))


@app.route("/owner/recurring-orders/sync-templates", methods=["POST"])
@require_owner
def sync_templates_from_recurring():
    """Recalculate shift_templates.quantity + min/max from recurring order totals."""
    import math
    tp = target_productivity()
    templates = g.db.execute(
        "SELECT * FROM shift_templates WHERE active=1"
    ).fetchall()
    rec_rows = g.db.execute("SELECT * FROM recurring_orders").fetchall()

    # sum quantities per weekday
    totals = {}
    for r in rec_rows:
        totals[r["weekday"]] = (
            totals.get(r["weekday"], 0)
            + r["qty_original"] + r["qty_matcha"]
            + r["qty_hojicha"] + r["qty_other"]
        )

    updated = 0
    for t in templates:
        qty = totals.get(t["weekday"], 0)
        if qty == 0:
            continue
        shift_hrs = (to_minutes(t["end_time"]) - to_minutes(t["start_time"])) / 60.0
        needed_ph  = qty / tp if tp else 0
        raw        = math.ceil(needed_ph / shift_hrs) if shift_hrs else 0
        new_min    = max(1, raw)
        new_max    = max(new_min, raw + 1) if raw > 0 else new_min
        g.db.execute(
            "UPDATE shift_templates SET quantity=?, min_people=?, max_people=? WHERE id=?",
            (qty, new_min, new_max, t["id"]),
        )
        updated += 1
    g.db.commit()
    flash(f"Shift templates updated for {updated} day(s) from recurring order totals.", "success")
    return redirect(url_for("recurring_orders"))


@app.route("/owner/settings/productivity", methods=["POST"])
@require_owner
def set_productivity():
    try:
        val = float(request.form.get("target_productivity", ""))
        if val <= 0:
            raise ValueError
        database.set_setting(g.db, "target_productivity", str(val))
        g.db.commit()
        flash(f"Target productivity set to {val} pieces / person-hour.", "success")
    except ValueError:
        flash("Target productivity must be a positive number.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/settings/piece-rate", methods=["POST"])
@require_owner
def set_piece_rate():
    try:
        val = float(request.form.get("piece_rate", ""))
        if val <= 0:
            raise ValueError
        database.set_setting(g.db, "piece_rate", f"{val:.2f}")
        g.db.commit()
        flash(f"Piece rate set to ${val:.2f} / daifuku.", "success")
    except ValueError:
        flash("Piece rate must be a positive number.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/settings/gusto-rate", methods=["POST"])
@require_owner
def set_gusto_rate():
    try:
        val = float(request.form.get("gusto_rate", ""))
        if val <= 0:
            raise ValueError
        database.set_setting(g.db, "gusto_rate", f"{val:.2f}")
        g.db.commit()
        flash(f"Gusto base rate set to ${val:.2f} / hr.", "success")
    except ValueError:
        flash("Gusto rate must be a positive number.", "error")
    return redirect(url_for("owner_dashboard"))


# ---------------------------------------------------------------------------
# Salary report + popup entries
# ---------------------------------------------------------------------------

@app.route("/owner/salary")
@require_owner
def salary_report():
    today = date.today()
    date_from = request.values.get("from") or today.replace(day=1).isoformat()
    date_to   = request.values.get("to")   or today.isoformat()

    pr   = piece_rate()
    gr   = gusto_rate()
    sp_p = strawberry_price()
    dt_p = delivery_transport_amount()

    # --- regular shift assignments ---
    rows = g.db.execute(
        """SELECT a.employee_id, e.name,
                  COALESCE(a.actual_start, a.start_time) AS a_start,
                  COALESCE(a.actual_end,   a.end_time)   AS a_end,
                  a.start_time, a.end_time,
                  a.actual_start, a.actual_end,
                  a.is_manager, a.strawberries_bought,
                  si.id AS instance_id, si.date,
                  t.weekday, t.label
             FROM assignments a
             JOIN employees e   ON e.id = a.employee_id
             JOIN shift_instances si ON si.id = a.shift_instance_id
             JOIN shift_templates t  ON t.id = si.template_id
            WHERE si.date BETWEEN ? AND ?
            ORDER BY si.date, e.name""",
        (date_from, date_to),
    ).fetchall()

    # per-shift totals (person-hours + pieces → hourly rate)
    instance_info = {}
    for r in rows:
        iid = r["instance_id"]
        if iid not in instance_info:
            instance_info[iid] = {
                "date": r["date"],
                "weekday": WEEKDAY_NAMES[r["weekday"]],
                "label": r["label"],
                "person_hours": 0.0,
                "pieces": 0,
                "rate": 0.0,
            }
        hrs = (to_minutes(r["a_end"]) - to_minutes(r["a_start"])) / 60.0
        instance_info[iid]["person_hours"] += hrs

    for iid, info in instance_info.items():
        info["pieces"] = production.day_totals(g.db, info["date"])["total"]
        if info["person_hours"] > 0 and info["pieces"] > 0:
            info["rate"] = (pr * info["pieces"]) / info["person_hours"]

    # per-employee data (shifts)
    employees_data = {}
    for r in rows:
        eid = r["employee_id"]
        if eid not in employees_data:
            employees_data[eid] = {
                "name": r["name"],
                "shifts": [],
                "popups": [],
                "total_hours": 0.0,
                "total_pay": 0.0,
                "total_transport": 0.0,
            }
        inst = instance_info[r["instance_id"]]
        hrs  = (to_minutes(r["a_end"]) - to_minutes(r["a_start"])) / 60.0
        base = hrs * inst["rate"]
        pay  = base * 1.05 if r["is_manager"] else base
        strawberries = r["strawberries_bought"] or 0
        strawberry_cost = strawberries * STRAWBERRY_PRICE
        confirmed = bool(r["actual_start"])
        employees_data[eid]["shifts"].append({
            "date":        inst["date"],
            "weekday":     inst["weekday"],
            "label":       inst["label"],
            "time_window": f"{r['a_start']}–{r['a_end']}",
            "confirmed":   confirmed,
            "hours":       round(hrs, 2),
            "pieces":      inst["pieces"],
            "rate":        round(inst["rate"], 2),
            "pay":         round(pay, 2),
            "is_manager":  bool(r["is_manager"]),
        })
        employees_data[eid]["total_hours"] += hrs
        employees_data[eid]["total_pay"]   += pay

    # --- popup entries for the period ---
    popup_rows = g.db.execute(
        """SELECT p.*, e.name AS emp_name
             FROM popups p JOIN employees e ON e.id = p.employee_id
            WHERE p.date BETWEEN ? AND ? AND p.status = 'approved'
            ORDER BY p.date, e.name""",
        (date_from, date_to),
    ).fetchall()

    for p in popup_rows:
        eid  = p["employee_id"]
        rate = p["hourly_rate"] if p["hourly_rate"] else gr  # fall back to gusto_rate
        pay  = round(p["hours"] * rate, 2)
        if eid not in employees_data:
            employees_data[eid] = {
                "name": p["emp_name"],
                "shifts": [],
                "popups": [],
                "total_hours": 0.0,
                "total_pay": 0.0,
                "total_transport": 0.0,
            }
        employees_data[eid]["popups"].append({
            "id":          p["id"],
            "date":        p["date"],
            "description": p["description"],
            "start_time":  p["start_time"],
            "end_time":    p["end_time"],
            "hours":       round(p["hours"], 2),
            "hourly_rate": rate,
            "pay":         pay,
            "transport":   p["transport"],
        })
        employees_data[eid]["total_hours"]     += p["hours"]
        employees_data[eid]["total_pay"]       += pay
        employees_data[eid]["total_transport"] += p["transport"]

    # all popup entries flat list (for the add form — needed for delete too)
    all_popups = [dict(p) for p in popup_rows]

    # --- strawberry purchases ---
    sp_rows = g.db.execute(
        """SELECT sp.*, e.name AS emp_name
             FROM strawberry_purchases sp JOIN employees e ON e.id = sp.employee_id
            WHERE sp.date BETWEEN ? AND ? AND sp.status = 'approved'
            ORDER BY sp.date, e.name""",
        (date_from, date_to),
    ).fetchall()

    for sp in sp_rows:
        eid  = sp["employee_id"]
        cost = sp_p  # 1 strawberry = strawberry_price per entry
        if eid not in employees_data:
            employees_data[eid] = {
                "name": sp["emp_name"], "shifts": [], "popups": [],
                "total_hours": 0.0, "total_pay": 0.0, "total_transport": 0.0,
            }
        employees_data[eid].setdefault("strawberry_purchases", []).append({
            "id":   sp["id"],
            "date": sp["date"],
            "cost": round(cost, 2),
        })
        employees_data[eid]["total_strawberry_cost"] = \
            employees_data[eid].get("total_strawberry_cost", 0.0) + cost

    # --- auto delivery transport for Wed (weekday=2) and Thu (weekday=3) ---
    # Formula: base_fee + per_stop_fee × num_stops
    # Both fees use the delivery_transport setting (currently $6 each).
    delivery_transport_rows = g.db.execute(
        """SELECT deliverer, COALESCE(delivery_date, date) AS deliver_on,
                  COUNT(*) AS stops
             FROM orders
            WHERE COALESCE(delivery_date, date) BETWEEN ? AND ?
              AND deliverer IS NOT NULL AND deliverer != ''
            GROUP BY deliverer, COALESCE(delivery_date, date)""",
        (date_from, date_to),
    ).fetchall()

    for dr in delivery_transport_rows:
        deliver_on = dr["deliver_on"]
        y, mo, day_ = (int(x) for x in deliver_on.split("-"))
        wday = date(y, mo, day_).weekday()
        # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4 — skip weekend deliveries
        if wday not in (0, 1, 2, 3, 4):
            continue
        emp_row = g.db.execute(
            "SELECT id, name FROM employees WHERE lower(name) = lower(?)",
            (dr["deliverer"],),
        ).fetchone()
        if not emp_row:
            continue
        stops = dr["stops"]
        # Wed/Thu: base + per_stop × stops;  Mon/Tue/Fri: per_stop × stops only
        has_base = wday in (2, 3)
        amount   = (dt_p + dt_p * stops) if has_base else (dt_p * stops)
        eid = emp_row["id"]
        if eid not in employees_data:
            employees_data[eid] = {
                "name": emp_row["name"],
                "shifts": [], "popups": [],
                "delivery_transports": [],
                "total_hours": 0.0, "total_pay": 0.0, "total_transport": 0.0,
            }
        employees_data[eid].setdefault("delivery_transports", []).append({
            "date":     deliver_on,
            "weekday":  WEEKDAY_NAMES[wday],
            "stops":    stops,
            "has_base": has_base,
            "amount":   round(amount, 2),
        })
        employees_data[eid]["total_transport"] += amount

    # finalize per-employee totals
    emp_list = []
    all_employees = g.db.execute(
        "SELECT id, name FROM employees WHERE active=1 ORDER BY name"
    ).fetchall()
    for emp in sorted(employees_data.values(), key=lambda e: e["name"]):
        emp.setdefault("delivery_transports", [])
        emp.setdefault("strawberry_purchases", [])
        emp["total_hours"]          = round(emp["total_hours"], 2)
        emp["total_pay"]            = round(emp["total_pay"],   2)
        emp["total_transport"]      = round(emp["total_transport"], 2)
        emp["total_strawberry_cost"]= round(emp.get("total_strawberry_cost", 0.0), 2)
        # Gusto hours: (wage pay + transport + strawberry purchases) / gusto_rate
        net = emp["total_pay"] + emp["total_transport"] + emp["total_strawberry_cost"]
        emp["net_pay"]     = round(max(net, 0.0), 2)
        emp["gusto_hours"] = round(emp["net_pay"] / gr, 2) if gr else 0.0
        emp_list.append(emp)

    grand_pay        = round(sum(e["total_pay"]             for e in emp_list), 2)
    grand_transport  = round(sum(e["total_transport"]       for e in emp_list), 2)
    grand_strawberry = round(sum(e["total_strawberry_cost"] for e in emp_list), 2)
    grand_gusto      = round(sum(e["gusto_hours"]           for e in emp_list), 2)

    return render_template(
        "salary.html",
        date_from=date_from, date_to=date_to,
        employees=emp_list,
        all_employees=all_employees,
        piece_rate=pr, gusto_rate=gr,
        strawberry_price=sp_p, delivery_transport=dt_p,
        grand_pay=grand_pay, grand_transport=grand_transport,
        grand_strawberry=grand_strawberry,
        grand_gusto=grand_gusto,
    )


# ---------------------------------------------------------------------------
# Employee requests — strawberry purchase & pop-up hours
# ---------------------------------------------------------------------------

@app.route("/my-requests/strawberry/add", methods=["POST"])
@require_login
def employee_request_strawberry():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))
    pdate = request.form.get("date", "").strip()
    if not pdate:
        flash("Date required.", "error")
        return redirect(url_for("my_shifts"))
    g.db.execute(
        "INSERT INTO strawberry_purchases (employee_id, date, quantity, status, requested_by, created_at)"
        " VALUES (?, ?, 1, 'pending', ?, ?)",
        (emp_id, pdate, emp_id, database.now_iso()),
    )
    g.db.commit()
    flash("Strawberry purchase request submitted — waiting for owner approval.", "success")
    return redirect(url_for("my_shifts"))


@app.route("/my-requests/popup/add", methods=["POST"])
@require_login
def employee_request_popup():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))
    try:
        pdate      = request.form.get("date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time   = request.form.get("end_time",   "").strip()
        desc       = request.form.get("description", "").strip()
        transport  = float(request.form.get("transport", "0") or 0)
        if not pdate or not start_time or not end_time:
            raise ValueError("date and times required")
        hours = (to_minutes(end_time) - to_minutes(start_time)) / 60.0
        if hours <= 0:
            raise ValueError("end time must be after start time")
        g.db.execute(
            """INSERT INTO popups
                 (employee_id, date, description, hours, hourly_rate, transport,
                  start_time, end_time, status, requested_by, created_at)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'pending', ?, ?)""",
            (emp_id, pdate, desc, round(hours, 4), transport,
             start_time, end_time, emp_id, database.now_iso()),
        )
        g.db.commit()
        flash("Pop-up/event request submitted — waiting for owner approval.", "success")
    except (ValueError, TypeError) as e:
        flash(f"Could not submit: {e}", "error")
    return redirect(url_for("my_shifts"))


@app.route("/owner/requests/decide", methods=["POST"])
@require_owner
def decide_request():
    """Approve or reject an employee strawberry or popup request."""
    kind   = request.form.get("kind")   # 'strawberry' or 'popup'
    rid    = request.form.get("id")
    action = request.form.get("action") # 'approve' or 'reject'
    if kind == "strawberry":
        g.db.execute(
            "UPDATE strawberry_purchases SET status=? WHERE id=?",
            (action + "d", rid),
        )
    elif kind == "popup":
        g.db.execute("UPDATE popups SET status=? WHERE id=?", (action + "d", rid))
    g.db.commit()
    flash(f"Request {action}d.", "success")
    return redirect(url_for("approvals"))


@app.route("/owner/salary/popup/add", methods=["POST"])
@require_owner
def add_popup():
    date_from = request.form.get("date_from", "")
    date_to   = request.form.get("date_to", "")
    try:
        emp_id     = int(request.form.get("employee_id", ""))
        start_time = request.form.get("start_time", "").strip()
        end_time   = request.form.get("end_time",   "").strip()
        rate       = float(request.form.get("hourly_rate", "0") or 0)
        transport  = float(request.form.get("transport", "0") or 0)
        pdate      = request.form.get("date", "").strip()
        desc       = request.form.get("description", "").strip()
        if not pdate or not start_time or not end_time:
            raise ValueError("date and start/end time are required")
        hours = (to_minutes(end_time) - to_minutes(start_time)) / 60.0
        if hours <= 0:
            raise ValueError("end time must be after start time")
        g.db.execute(
            """INSERT INTO popups
                 (employee_id, date, description, hours, hourly_rate, transport,
                  start_time, end_time, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?)""",
            (emp_id, pdate, desc, round(hours, 4), rate, transport,
             start_time, end_time, database.now_iso()),
        )
        g.db.commit()
        flash("Pop-up entry added.", "success")
    except (ValueError, TypeError) as e:
        flash(f"Could not add entry: {e}", "error")
    return redirect(url_for("salary_report", **{"from": date_from, "to": date_to}))


@app.route("/owner/salary/popup/<int:popup_id>/delete", methods=["POST"])
@require_owner
def delete_popup(popup_id):
    date_from = request.form.get("date_from", "")
    date_to   = request.form.get("date_to", "")
    g.db.execute("DELETE FROM popups WHERE id = ?", (popup_id,))
    g.db.commit()
    flash("Pop-up entry removed.", "success")
    return redirect(url_for("salary_report", **{"from": date_from, "to": date_to}))


@app.route("/owner/salary/strawberry/add", methods=["POST"])
@require_owner
def add_strawberry_purchase():
    date_from = request.form.get("date_from", "")
    date_to   = request.form.get("date_to", "")
    try:
        emp_id = int(request.form.get("employee_id", ""))
        pdate  = request.form.get("date", "").strip()
        if not pdate:
            raise ValueError("date required")
        g.db.execute(
            "INSERT INTO strawberry_purchases (employee_id, date, quantity, status, created_at)"
            " VALUES (?, ?, 1, 'approved', ?)",
            (emp_id, pdate, database.now_iso()),
        )
        g.db.commit()
        flash("Strawberry purchase recorded.", "success")
    except (ValueError, TypeError) as e:
        flash(f"Could not add entry: {e}", "error")
    return redirect(url_for("salary_report", **{"from": date_from, "to": date_to}))


@app.route("/owner/salary/strawberry/<int:purchase_id>/delete", methods=["POST"])
@require_owner
def delete_strawberry_purchase(purchase_id):
    date_from = request.form.get("date_from", "")
    date_to   = request.form.get("date_to", "")
    g.db.execute("DELETE FROM strawberry_purchases WHERE id = ?", (purchase_id,))
    g.db.commit()
    flash("Strawberry purchase removed.", "success")
    return redirect(url_for("salary_report", **{"from": date_from, "to": date_to}))


@app.route("/owner/guide")
@require_owner
def owner_guide():
    return render_template(
        "guide.html",
        piece_rate=piece_rate(),
        gusto_rate=gusto_rate(),
    )


@app.route("/owner/clients/add", methods=["POST"])
@require_owner
def add_client():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Client name required.", "error")
    else:
        try:
            g.db.execute("INSERT INTO clients (name) VALUES (?)", (name,))
            g.db.commit()
            flash(f"Added client {name}.", "success")
        except Exception:
            flash(f"Client {name} already exists.", "error")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/clients/<int:client_id>/toggle", methods=["POST"])
@require_owner
def toggle_client(client_id):
    g.db.execute("UPDATE clients SET active = 1 - active WHERE id = ?", (client_id,))
    g.db.commit()
    flash("Client status updated.", "success")
    return redirect(url_for("owner_dashboard"))


@app.route("/owner/orders")
@require_owner
def orders_month():
    month = request.values.get("month") or next_month_str()
    instances = instances_for_month(month)
    rows = []
    for inst in instances:
        totals = production.day_totals(g.db, inst["date"])
        rows.append({
            "inst": inst,
            "weekday": WEEKDAY_NAMES[inst["weekday"]],
            "totals": totals,
        })
    return render_template(
        "orders_month.html",
        month=month, month_label=month_label(month),
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        rows=rows, flavors=FLAVORS,
    )


@app.route("/owner/orders/<date>", methods=["GET", "POST"])
@require_owner
def orders_day(date):
    inst = g.db.execute(
        """SELECT si.id AS instance_id, si.date, t.*
             FROM shift_instances si JOIN shift_templates t ON t.id = si.template_id
            WHERE si.date = ?""",
        (date,),
    ).fetchone()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            client_id = request.form.get("client_id")
            if not client_id:
                flash("Pick a client.", "error")
            else:
                qtys = [_int(request.form.get(f"qty_{s}")) for s, _ in FLAVORS]
                deliver_on = request.form.get("delivery_date", "").strip() or date
                g.db.execute(
                    """INSERT INTO orders
                         (client_id, date, qty_original, qty_matcha, qty_hojicha,
                          qty_other, deliverer, note, delivery_date, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, date, *qtys,
                     request.form.get("deliverer", "").strip(),
                     request.form.get("note", "").strip(), deliver_on, database.now_iso()),
                )
                g.db.commit()
                flash("Order added.", "success")
        elif action == "update":
            order_id = request.form.get("order_id")
            qtys = [_int(request.form.get(f"qty_{s}")) for s, _ in FLAVORS]
            g.db.execute(
                """UPDATE orders
                      SET qty_original=?, qty_matcha=?, qty_hojicha=?, qty_other=?,
                          deliverer=?, note=?, delivery_date=?
                    WHERE id=? AND date=?""",
                (*qtys,
                 request.form.get("deliverer", "").strip(),
                 request.form.get("note", "").strip(),
                 request.form.get("delivery_date", "").strip() or date,
                 order_id, date),
            )
            g.db.commit()
            flash("Order updated.", "success")
        elif action == "delete":
            g.db.execute("DELETE FROM orders WHERE id = ? AND date = ?",
                         (request.form.get("order_id"), date))
            g.db.commit()
            flash("Order removed.", "success")
        return redirect(url_for("orders_day", date=date))

    orders = production.orders_for_date(g.db, date)
    order_rows = [{**dict(o), "total": production.order_total(o)} for o in orders]
    clients = g.db.execute(
        "SELECT id, name FROM clients WHERE active = 1 ORDER BY name").fetchall()
    employees = g.db.execute(
        "SELECT id, name FROM employees WHERE active=1 ORDER BY name").fetchall()
    totals = production.day_totals(g.db, date)

    # Build per-client suggested delivery dates from recurring offsets
    from datetime import timedelta as _td
    wday = date_weekday(date)
    y, mo, dy = (int(x) for x in date.split("-"))
    prod_date = date.__class__(y, mo, dy)
    rec_offsets = g.db.execute(
        "SELECT client_id, delivery_offset FROM recurring_orders WHERE weekday = ?",
        (wday,),
    ).fetchall()
    # {client_id: suggested_delivery_date_iso}
    delivery_suggestions = {
        r["client_id"]: (prod_date + _td(days=r["delivery_offset"] or 0)).isoformat()
        for r in rec_offsets
    }

    return render_template(
        "orders_day.html",
        date=date, inst=inst, orders=order_rows, clients=clients,
        employees=employees,
        totals=totals, flavors=FLAVORS,
        weekday=WEEKDAY_NAMES[wday],
        delivery_suggestions=delivery_suggestions,
    )


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def date_weekday(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return date(y, m, d).weekday()


@app.route("/owner/deliveries", methods=["GET", "POST"])
@require_owner
def deliveries_month():
    month = request.values.get("month") or next_month_str()

    if request.method == "POST":
        # Update blackout flags
        # First clear all blackouts for this month, then re-insert checked ones
        g.db.execute(
            "DELETE FROM delivery_blackout WHERE date LIKE ?", (month + "-%",)
        )
        for key in request.form:
            if key.startswith("no_del_"):
                g.db.execute(
                    "INSERT INTO delivery_blackout (date) VALUES (?) ON CONFLICT DO NOTHING",
                    (key[len("no_del_"):],),
                )
        # Per-client per-date deliverer assignment
        for key, deliverer in request.form.items():
            if not key.startswith("dlv_"):
                continue
            # key format: dlv_{deliver_on}_{client_id}
            _, deliver_on, client_id = key.split("_", 2)
            g.db.execute(
                "UPDATE orders SET deliverer=? WHERE client_id=? AND COALESCE(delivery_date, date)=?",
                (deliverer.strip(), client_id, deliver_on),
            )
        g.db.commit()
        flash("Deliverer assignments saved.", "success")
        return redirect(url_for("deliveries_month", month=month))

    # All clients per delivery date (one row per client per date)
    client_rows = g.db.execute(
        """SELECT COALESCE(o.delivery_date, o.date) AS deliver_on,
                  o.client_id, c.name AS client_name,
                  SUM(o.qty_original+o.qty_matcha+o.qty_hojicha+o.qty_other) AS total,
                  MAX(o.delivered)  AS delivered,
                  MAX(o.deliverer)  AS deliverer
             FROM orders o JOIN clients c ON c.id = o.client_id
            WHERE COALESCE(o.delivery_date, o.date) LIKE ?
            GROUP BY deliver_on, o.client_id, c.name
            ORDER BY deliver_on, c.name""",
        (month + "-%",),
    ).fetchall()

    # Employees available to deliver — from production shift can_deliver flag
    can_deliver_rows = g.db.execute(
        """SELECT DISTINCT COALESCE(o.delivery_date, o.date) AS deliver_on, e.name AS emp_name
             FROM availability av
             JOIN employees e ON e.id = av.employee_id
             JOIN shift_instances si ON si.id = av.shift_instance_id
             JOIN orders o ON o.date = si.date
            WHERE av.can_deliver = 1
              AND COALESCE(o.delivery_date, o.date) LIKE ?
            ORDER BY deliver_on, e.name""",
        (month + "-%",),
    ).fetchall()
    can_deliver_map = {}
    for r in can_deliver_rows:
        can_deliver_map.setdefault(r["deliver_on"], []).append(r["emp_name"])

    # Also include employees who signed up via delivery_availability (e.g. Thursdays)
    del_av_rows = g.db.execute(
        """SELECT da.date AS deliver_on, e.name AS emp_name
             FROM delivery_availability da
             JOIN employees e ON e.id = da.employee_id
            WHERE da.date LIKE ?
            ORDER BY da.date, e.name""",
        (month + "-%",),
    ).fetchall()
    for r in del_av_rows:
        names = can_deliver_map.setdefault(r["deliver_on"], [])
        if r["emp_name"] not in names:
            names.append(r["emp_name"])

    employees = g.db.execute(
        "SELECT id, name FROM employees WHERE active=1 ORDER BY name"
    ).fetchall()

    blackout_dates = {
        r["date"] for r in g.db.execute(
            "SELECT date FROM delivery_blackout WHERE date LIKE ?", (month + "-%",)
        ).fetchall()
    }

    # Group clients by delivery date
    rows_by_date = {}
    for r in client_rows:
        d = r["deliver_on"]
        available = can_deliver_map.get(d, [])
        # Auto-assign: use first available person if no deliverer saved yet
        saved_deliverer = r["deliverer"] or ""
        auto_deliverer = saved_deliverer or (available[0] if available else "")
        if d not in rows_by_date:
            rows_by_date[d] = {
                "date":           d,
                "weekday":        WEEKDAY_NAMES[date_weekday(d)],
                "clients":        [],
                "can_deliver":    available,
                "pcs":            0,
                "n_done":         0,
                "n_total":        0,
                "no_delivery":    d in blackout_dates,
                "auto_deliverer": auto_deliverer,
            }
        rows_by_date[d]["clients"].append({
            "client_id":   r["client_id"],
            "client_name": r["client_name"],
            "total":       r["total"] or 0,
            "deliverer":   auto_deliverer,
            "delivered":   bool(r["delivered"]),
        })
        rows_by_date[d]["pcs"]     += r["total"] or 0
        rows_by_date[d]["n_total"] += 1
        rows_by_date[d]["n_done"]  += 1 if r["delivered"] else 0

    rows = sorted(rows_by_date.values(), key=lambda x: x["date"])

    return render_template(
        "deliveries_month.html",
        month=month, month_label=month_label(month),
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        rows=rows, employees=employees,
    )


@app.route("/owner/deliveries/<date>", methods=["GET", "POST"])
@require_owner
def deliveries_day(date):
    if request.method == "POST":
        # Form is keyed by client_id; each client group updates ALL its orders at once
        client_ids = request.form.getlist("client_id")
        for cid in client_ids:
            deliverer  = request.form.get(f"deliverer_{cid}", "").strip()
            delivered  = 1 if request.form.get(f"delivered_{cid}") else 0
            reschedule = request.form.get(f"deliver_on_{cid}", "").strip() or date
            g.db.execute(
                """UPDATE orders SET deliverer=?, delivered=?, delivery_date=?
                    WHERE client_id=? AND COALESCE(delivery_date, date)=?""",
                (deliverer, delivered, reschedule, cid, date),
            )
        g.db.commit()
        flash("Deliveries updated.", "success")
        return redirect(url_for("deliveries_day", date=date))

    raw = [{**dict(o), "total": production.order_total(o)}
           for o in production.deliveries_for_date(g.db, date)]

    # Group by client — combine quantities, unify delivered/deliverer status
    groups = {}
    for o in raw:
        cid = o["client_id"]
        if cid not in groups:
            groups[cid] = {
                "client_id":   cid,
                "client_name": o["client_name"],
                "deliver_on":  o["deliver_on"],
                "delivered":   0,
                "deliverer":   o["deliverer"] or "",
                "qty_original": 0, "qty_matcha": 0,
                "qty_hojicha":  0, "qty_other":  0,
                "total": 0,
                "prod_dates": [],
            }
        grp = groups[cid]
        for f in ("qty_original", "qty_matcha", "qty_hojicha", "qty_other"):
            grp[f] += (o[f] or 0)
        grp["total"]    += o["total"]
        grp["delivered"] = max(grp["delivered"], o["delivered"] or 0)
        if o["deliverer"]:
            grp["deliverer"] = o["deliverer"]
        if o["date"] not in grp["prod_dates"]:
            grp["prod_dates"].append(o["date"])

    rows = sorted(groups.values(), key=lambda r: (r["delivered"], r["client_name"]))
    pcs  = sum(r["total"] for r in rows)

    employees = g.db.execute(
        "SELECT id, name FROM employees WHERE active=1 ORDER BY name"
    ).fetchall()
    return render_template(
        "deliveries_day.html",
        date=date, weekday=WEEKDAY_NAMES[date_weekday(date)],
        rows=rows, total_pcs=pcs, flavors=FLAVORS,
        n_done=sum(1 for r in rows if r["delivered"]),
        employees=employees,
    )


# ---------------------------------------------------------------------------
# Owner — approvals
# ---------------------------------------------------------------------------

@app.route("/owner/approvals", methods=["GET", "POST"])
@require_owner
def approvals():
    if request.method == "POST":
        action = request.form.get("action")
        ids = request.form.getlist("av_id")
        def _notify_employees(av_ids, decision):
            """Send a Slack message for each affected employee listing their shifts."""
            if not av_ids:
                return
            rows = g.db.execute(
                f"""SELECT e.name AS emp_name, si.date, t.label, t.weekday
                      FROM availability av
                      JOIN employees e  ON e.id  = av.employee_id
                      JOIN shift_instances si ON si.id = av.shift_instance_id
                      JOIN shift_templates t  ON t.id  = si.template_id
                     WHERE av.id IN ({','.join('?' for _ in av_ids)})
                     ORDER BY e.name, si.date""",
                av_ids,
            ).fetchall()
            by_emp = {}
            for r in rows:
                by_emp.setdefault(r["emp_name"], []).append(
                    f"{WEEKDAY_NAMES[r['weekday']]} {r['date']} ({r['label']})"
                )
            icon = "✅" if decision == "approved" else "❌"
            lines = []
            for emp, shifts in by_emp.items():
                lines.append(f"{icon} *{emp}* — availability {decision}:")
                for s in shifts:
                    lines.append(f"  • {s}")
            _send_slack("\n".join(lines))

        if action == "approve_originals":
            rows_to_approve = g.db.execute(
                "SELECT id FROM availability WHERE status='pending' AND is_update=0"
            ).fetchall()
            ids_to_approve = [r["id"] for r in rows_to_approve]
            if ids_to_approve:
                g.db.executemany(
                    "UPDATE availability SET status='approved', decided_at=? WHERE id=?",
                    [(database.now_iso(), i) for i in ids_to_approve],
                )
                g.db.commit()
                _notify_employees(ids_to_approve, "approved")
                flash(f"{len(ids_to_approve)} original submission(s) approved.", "success")
            else:
                flash("No original submissions to approve.", "info")
        elif action in ("approve", "reject") and ids:
            status = "approved" if action == "approve" else "rejected"
            g.db.executemany(
                "UPDATE availability SET status = ?, decided_at = ? WHERE id = ?",
                [(status, database.now_iso(), i) for i in ids],
            )
            g.db.commit()
            _notify_employees(ids, status)
            flash(f"{len(ids)} submission(s) {status}.", "success")
        return redirect(url_for("approvals", status=request.values.get("status", "pending")))

    status = request.values.get("status", "pending")
    rows = g.db.execute(
        """SELECT av.id, av.start_time, av.end_time, av.status, av.note,
                  av.submitted_at, av.is_update, av.if_needed, e.name AS emp_name, si.date,
                  t.label, t.start_time AS shift_start, t.end_time AS shift_end, t.weekday
             FROM availability av
             JOIN employees e ON e.id = av.employee_id
             JOIN shift_instances si ON si.id = av.shift_instance_id
             JOIN shift_templates t ON t.id = si.template_id
            WHERE av.status = ?
            ORDER BY av.is_update, si.date, t.start_time, e.name""",
        (status,),
    ).fetchall()
    counts = {
        r["status"]: r["n"]
        for r in g.db.execute(
            "SELECT status, COUNT(*) AS n FROM availability GROUP BY status"
        ).fetchall()
    }
    # Pending employee requests for strawberry purchases and pop-up hours
    pending_strawberries = g.db.execute(
        """SELECT sp.id, sp.date, sp.created_at, e.name AS emp_name
             FROM strawberry_purchases sp JOIN employees e ON e.id = sp.employee_id
            WHERE sp.status = 'pending'
            ORDER BY sp.date, e.name"""
    ).fetchall()

    pending_popups = g.db.execute(
        """SELECT p.id, p.date, p.description, p.start_time, p.end_time,
                  p.hours, p.transport, p.created_at, e.name AS emp_name
             FROM popups p JOIN employees e ON e.id = p.employee_id
            WHERE p.status = 'pending'
            ORDER BY p.date, e.name"""
    ).fetchall()

    pending_shift_reports = g.db.execute(
        """SELECT sr.id, sr.submitted_at, sr.strawberry_stock, sr.anko_stock, sr.memo,
                  si.date, t.label, t.weekday,
                  e.name AS submitter
             FROM shift_reports sr
             JOIN shift_instances si ON si.id = sr.shift_instance_id
             JOIN shift_templates t  ON t.id  = si.template_id
             JOIN employees e        ON e.id  = sr.submitted_by
            WHERE sr.status = 'pending'
            ORDER BY si.date"""
    ).fetchall()

    return render_template(
        "approvals.html", rows=rows, status=status, counts=counts,
        weekday_names=WEEKDAY_NAMES,
        pending_strawberries=pending_strawberries,
        pending_popups=pending_popups,
        pending_shift_reports=pending_shift_reports,
    )


# ---------------------------------------------------------------------------
# Owner — schedule overview + per-shift assignment
# ---------------------------------------------------------------------------

@app.route("/owner/schedule")
@require_owner
def owner_schedule():
    month = request.values.get("month") or next_month_str()
    tp = target_productivity()
    instances = instances_for_month(month)
    rows = []
    for inst in instances:
        people = assigned_people(inst["instance_id"])
        cov = coverage(inst, [dict(p) for p in people])
        total = production.day_totals(g.db, inst["date"])["total"]
        staff = production.staffing(inst, total, [dict(p) for p in people], tp)
        rows.append({
            "inst": inst,
            "weekday": WEEKDAY_NAMES[inst["weekday"]],
            "n_assigned": len(people),
            "n_candidates": len(approved_candidates(inst["instance_id"])),
            "cov": cov,
            "total_pieces": total,
            "staff": staff,
        })
    return render_template(
        "schedule.html",
        month=month, month_label=month_label(month),
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        rows=rows, target_productivity=tp,
    )


@app.route("/owner/schedule/auto-month", methods=["POST"])
@require_owner
def auto_assign_month():
    """Auto-assign every shift in the month at once.

    mode='empty'  → only fill shifts that have no assignments yet (won't touch manual work)
    mode='all'    → re-assign every shift, overwriting existing assignments
    """
    month = request.form.get("month") or next_month_str()
    mode = request.form.get("mode", "empty")
    instances = instances_for_month(month)

    assigned_total, shifts_done, skipped = 0, 0, 0
    for inst in instances:
        iid = inst["instance_id"]
        if mode == "empty" and assigned_people(iid):
            skipped += 1
            continue
        chosen = auto_assign(inst, approved_candidates(iid))
        g.db.execute("DELETE FROM assignments WHERE shift_instance_id = ?", (iid,))
        g.db.executemany(
            "INSERT INTO assignments (shift_instance_id, employee_id, start_time, end_time)"
            " VALUES (?, ?, ?, ?)",
            [(iid, c["employee_id"], c["start"], c["end"]) for c in chosen],
        )
        assigned_total += len(chosen)
        shifts_done += 1
    g.db.commit()

    msg = f"{month_label(month)}: staffed {shifts_done} shift(s), {assigned_total} assignment(s)."
    if skipped:
        msg += f" Left {skipped} already-assigned shift(s) untouched."
    flash(msg, "success")
    return redirect(url_for("owner_schedule", month=month))


@app.route("/owner/schedule/<int:instance_id>", methods=["GET", "POST"])
@require_owner
def shift_detail(instance_id):
    inst = g.db.execute(
        """SELECT si.id AS instance_id, si.date, t.*
             FROM shift_instances si JOIN shift_templates t ON t.id = si.template_id
            WHERE si.id = ?""",
        (instance_id,),
    ).fetchone()
    if inst is None:
        flash("Shift not found.", "error")
        return redirect(url_for("owner_schedule"))

    candidates = approved_candidates(instance_id)
    cand_by_id = {c["employee_id"]: c for c in candidates}

    if request.method == "POST":
        action = request.form.get("action")
        default_mgr = weekday_manager_id(inst["weekday"])
        if action == "auto":
            chosen = auto_assign(inst, candidates)
            chosen_ids = {c["employee_id"] for c in chosen}
            # If the weekday manager has approved availability but wasn't picked,
            # force-add them.
            if default_mgr and default_mgr not in chosen_ids:
                mgr_cand = next((c for c in candidates if c["employee_id"] == default_mgr), None)
                if mgr_cand:
                    chosen.append(mgr_cand)
            g.db.execute("DELETE FROM assignments WHERE shift_instance_id = ?", (instance_id,))
            g.db.executemany(
                "INSERT INTO assignments (shift_instance_id, employee_id, start_time, end_time, is_manager)"
                " VALUES (?, ?, ?, ?, ?)",
                [(instance_id, c["employee_id"], c["start"], c["end"],
                  1 if c["employee_id"] == default_mgr else 0) for c in chosen],
            )
            g.db.commit()
            flash(f"Auto-assigned {len(chosen)} person(s).", "success")
        elif action == "save":
            chosen_ids = request.form.getlist("include")
            manager_ids = set(int(x) for x in request.form.getlist("manager"))
            # Always include the default weekday manager in manager_ids
            if default_mgr:
                manager_ids.add(default_mgr)
            g.db.execute("DELETE FROM assignments WHERE shift_instance_id = ?", (instance_id,))
            errors = []
            for cid in chosen_ids:
                cid = int(cid)
                cand = cand_by_id.get(cid)
                if not cand:
                    continue
                start = request.form.get(f"start_{cid}") or cand["start"]
                end = request.form.get(f"end_{cid}") or cand["end"]
                if not _within(start, end, inst):
                    errors.append(f"{cand['name']}: window must be within shift hours.")
                    continue
                g.db.execute(
                    "INSERT INTO assignments (shift_instance_id, employee_id, start_time, end_time, is_manager)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (instance_id, cid, start, end, 1 if cid in manager_ids else 0),
                )
            g.db.commit()
            for e in errors:
                flash(e, "error")
            if not errors:
                flash("Assignment saved.", "success")
        return redirect(url_for("shift_detail", instance_id=instance_id))

    assigned = {a["employee_id"]: a for a in assigned_people(instance_id)}
    cov = coverage(inst, [dict(a) for a in assigned.values()])

    # Build candidate rows: start from approved availability, then add any
    # assigned people who aren't in that list (assigned without approval).
    cand_ids = {c["employee_id"] for c in candidates}
    merged_candidates = list(candidates)
    for a in assigned.values():
        if a["employee_id"] not in cand_ids:
            merged_candidates.append({
                "employee_id": a["employee_id"],
                "name":        a["name"],
                "start":       a["start"],
                "end":         a["end"],
            })
    merged_candidates.sort(key=lambda c: c["name"])

    cand_rows = []
    for c in merged_candidates:
        a = assigned.get(c["employee_id"])
        cand_rows.append({
            **c,
            "included":       a is not None,
            "assigned_start": a["start"]      if a else c["start"],
            "assigned_end":   a["end"]        if a else c["end"],
            "is_manager":     bool(a["is_manager"]) if a else False,
        })

    # Shift report status (submitted by manager)
    shift_report = g.db.execute(
        """SELECT sr.status, sr.submitted_at, sr.decided_at, e.name AS submitter
             FROM shift_reports sr JOIN employees e ON e.id = sr.submitted_by
            WHERE sr.shift_instance_id = ?""",
        (instance_id,),
    ).fetchone()

    totals = production.day_totals(g.db, inst["date"])
    orders = [{**dict(o), "total": production.order_total(o)}
              for o in production.orders_for_date(g.db, inst["date"])]
    staff = production.staffing(
        inst, totals["total"], [dict(a) for a in assigned.values()], target_productivity())
    return render_template(
        "shift_detail.html",
        inst=inst, weekday=WEEKDAY_NAMES[inst["weekday"]],
        candidates=cand_rows, cov=cov,
        shift_report=shift_report,
        totals=totals, orders=orders, staff=staff, flavors=FLAVORS,
        strawberry_price=STRAWBERRY_PRICE,
    )


# ---------------------------------------------------------------------------
# Employee — delivery sign-off
# ---------------------------------------------------------------------------

@app.route("/my-deliveries", methods=["GET", "POST"])
@require_login
def my_deliveries():
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))

    emp_name = session.get("employee_name", "")

    if request.method == "POST":
        deliver_on = request.form.get("deliver_on")
        client_id  = request.form.get("client_id")
        delivered  = 1 if request.form.get("delivered") else 0
        if deliver_on and client_id:
            g.db.execute(
                """UPDATE orders SET delivered=?
                    WHERE client_id=? AND COALESCE(delivery_date, date)=?
                      AND lower(deliverer)=lower(?)""",
                (delivered, client_id, deliver_on, emp_name),
            )
            g.db.commit()
            if delivered:
                client_name = g.db.execute(
                    "SELECT name FROM clients WHERE id=?", (client_id,)
                ).fetchone()["name"]
                webhook = database.get_setting(g.db, "slack_webhook_delivery", "")
                if webhook:
                    msg = f"🚚 *{client_name}* delivery done — {deliver_on}"
                    try:
                        data = _json.dumps({"text": msg}).encode()
                        req = urllib.request.Request(
                            webhook, data=data,
                            headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(req, timeout=5)
                    except Exception:
                        pass
            flash("Delivery status updated.", "success")
        return redirect(url_for("my_deliveries"))

    # Fetch all upcoming + recent deliveries assigned to this employee
    rows = g.db.execute(
        """SELECT COALESCE(o.delivery_date, o.date) AS deliver_on,
                  o.client_id, c.name AS client_name,
                  SUM(o.qty_original) AS qty_original,
                  SUM(o.qty_matcha)   AS qty_matcha,
                  SUM(o.qty_hojicha)  AS qty_hojicha,
                  SUM(o.qty_other)    AS qty_other,
                  SUM(o.qty_original+o.qty_matcha+o.qty_hojicha+o.qty_other) AS total,
                  MAX(o.delivered)    AS delivered
             FROM orders o JOIN clients c ON c.id = o.client_id
            WHERE lower(o.deliverer) = lower(?)
              AND COALESCE(o.delivery_date, o.date) >= (CURRENT_DATE - INTERVAL '7 days')::text
            GROUP BY deliver_on, o.client_id, c.name
            ORDER BY deliver_on, c.name""",
        (emp_name,),
    ).fetchall()

    # Group by date
    by_date = {}
    for r in rows:
        d = r["deliver_on"]
        if d not in by_date:
            by_date[d] = {
                "date":    d,
                "weekday": WEEKDAY_NAMES[date_weekday(d)],
                "clients": [],
                "n_done":  0,
                "n_total": 0,
            }
        by_date[d]["clients"].append(dict(r))
        by_date[d]["n_total"] += 1
        by_date[d]["n_done"]  += 1 if r["delivered"] else 0

    delivery_days = sorted(by_date.values(), key=lambda x: x["date"])
    today_str = date.today().isoformat()

    return render_template(
        "my_deliveries.html",
        delivery_days=delivery_days, today=today_str, flavors=FLAVORS,
    )


# ---------------------------------------------------------------------------
# Manager shift reports
# ---------------------------------------------------------------------------

def _send_slack(message: str):
    webhook = database.get_setting(g.db, "slack_webhook", "")
    if not webhook:
        return
    try:
        data = _json.dumps({"text": message}).encode()
        req = urllib.request.Request(webhook, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # don't crash if Slack is unreachable


@app.route("/my-shift/<int:instance_id>/report", methods=["GET", "POST"])
@require_login
def shift_report(instance_id):
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect(url_for("owner_dashboard"))

    # Must be the assigned manager for this shift
    manager_row = g.db.execute(
        "SELECT * FROM assignments WHERE shift_instance_id=? AND employee_id=? AND is_manager=1",
        (instance_id, emp_id),
    ).fetchone()
    if not manager_row:
        flash("Only the assigned manager can submit a shift report.", "error")
        return redirect(url_for("my_shifts"))

    inst = g.db.execute(
        """SELECT si.id AS instance_id, si.date, t.label, t.start_time, t.end_time, t.weekday
             FROM shift_instances si JOIN shift_templates t ON t.id = si.template_id
            WHERE si.id = ?""",
        (instance_id,),
    ).fetchone()

    # All workers assigned to this shift
    workers = g.db.execute(
        """SELECT a.employee_id, e.name, a.start_time AS sched_start, a.end_time AS sched_end,
                  a.actual_start, a.actual_end, a.is_manager
             FROM assignments a JOIN employees e ON e.id = a.employee_id
            WHERE a.shift_instance_id = ?
            ORDER BY a.is_manager DESC, e.name""",
        (instance_id,),
    ).fetchall()

    # Existing report if any
    existing = g.db.execute(
        "SELECT * FROM shift_reports WHERE shift_instance_id=?", (instance_id,)
    ).fetchone()
    existing_hours = {}
    if existing:
        for r in g.db.execute(
            "SELECT * FROM shift_report_hours WHERE report_id=?", (existing["id"],)
        ).fetchall():
            existing_hours[r["employee_id"]] = r

    if request.method == "POST":
        if existing and existing["status"] == "approved":
            flash("This report has already been approved and cannot be changed.", "error")
            return redirect(url_for("shift_report", instance_id=instance_id))

        strawberry = request.form.get("strawberry_stock", "").strip()
        anko = request.form.get("anko_stock", "").strip()
        memo = request.form.get("memo", "").strip()

        # Upsert the report
        g.db.execute(
            """INSERT INTO shift_reports
                 (shift_instance_id, submitted_by, status, strawberry_stock, anko_stock, memo, submitted_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?)
               ON CONFLICT(shift_instance_id) DO UPDATE SET
                 submitted_by=excluded.submitted_by,
                 status='pending',
                 strawberry_stock=excluded.strawberry_stock,
                 anko_stock=excluded.anko_stock,
                 memo=excluded.memo,
                 submitted_at=excluded.submitted_at,
                 decided_at=NULL""",
            (instance_id, emp_id,
             strawberry or None, anko or None,
             memo, database.now_iso()),
        )
        report = g.db.execute(
            "SELECT id FROM shift_reports WHERE shift_instance_id=?", (instance_id,)
        ).fetchone()
        report_id = report["id"]

        for w in workers:
            wid = w["employee_id"]
            start = request.form.get(f"actual_start_{wid}", "").strip()
            end = request.form.get(f"actual_end_{wid}", "").strip()
            if start and end:
                g.db.execute(
                    """INSERT INTO shift_report_hours (report_id, employee_id, actual_start, actual_end)
                         VALUES (?, ?, ?, ?)
                         ON CONFLICT(report_id, employee_id) DO UPDATE SET
                           actual_start=excluded.actual_start, actual_end=excluded.actual_end""",
                    (report_id, wid, start, end),
                )
        g.db.commit()

        # Send inventory + memo to Slack immediately (no approval needed)
        weekday_name = WEEKDAY_NAMES[inst["weekday"]]
        submitter_name = session.get("employee_name", "Manager")
        lines = [
            f"📋 *Shift Report — {weekday_name} {inst['date']} · {inst['label']}*",
            f"_Submitted by {submitter_name}_",
            "",
        ]
        if strawberry:
            lines.append(f"🍓 Strawberries remaining: *{strawberry}*")
        if anko:
            lines.append(f"🫘 Anko remaining: *{anko}*")
        if memo:
            lines.append(f"\n📝 Memo: {memo}")
        if strawberry or anko or memo:
            _send_slack("\n".join(lines))

        flash("Report submitted — inventory & memo sent to Slack. The owner will confirm hours.", "success")
        return redirect(url_for("my_shifts"))

    manager_early = to_hhmm(max(0, to_minutes(inst["start_time"]) - 15))
    return render_template(
        "shift_report.html",
        inst=inst, weekday=WEEKDAY_NAMES[inst["weekday"]],
        workers=workers, existing=existing, existing_hours=existing_hours,
        manager_early=manager_early,
    )


@app.route("/owner/shift-report/<int:report_id>/decide", methods=["POST"])
@require_owner
def decide_shift_report(report_id):
    action = request.form.get("action")
    report = g.db.execute("SELECT * FROM shift_reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        flash("Report not found.", "error")
        return redirect(url_for("approvals"))

    inst = g.db.execute(
        """SELECT si.date, t.label, t.weekday
             FROM shift_instances si JOIN shift_templates t ON t.id=si.template_id
            WHERE si.id=?""",
        (report["shift_instance_id"],),
    ).fetchone()
    submitter = g.db.execute(
        "SELECT name FROM employees WHERE id=?", (report["submitted_by"],)
    ).fetchone()

    if action == "approve":
        # Write actual hours to assignments
        hours = g.db.execute(
            "SELECT * FROM shift_report_hours WHERE report_id=?", (report_id,)
        ).fetchall()
        for h in hours:
            g.db.execute(
                "UPDATE assignments SET actual_start=?, actual_end=? WHERE shift_instance_id=? AND employee_id=?",
                (h["actual_start"], h["actual_end"], report["shift_instance_id"], h["employee_id"]),
            )
        g.db.execute(
            "UPDATE shift_reports SET status='approved', decided_at=? WHERE id=?",
            (database.now_iso(), report_id),
        )
        g.db.commit()
        flash("Hours confirmed.", "success")

    elif action == "reject":
        g.db.execute(
            "UPDATE shift_reports SET status='rejected', decided_at=? WHERE id=?",
            (database.now_iso(), report_id),
        )
        g.db.commit()
        flash("Report rejected. The manager can resubmit.", "success")

    return redirect(url_for("approvals"))


if __name__ == "__main__":
    database.init_db()
    # macOS uses port 5000 for AirPlay Receiver, so default to 5001.
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=True)
