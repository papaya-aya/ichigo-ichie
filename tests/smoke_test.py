"""Render every owner page and check the money maths.

Catches the class of bug that only shows up at render time — a url_for with a
None argument, a missing template variable, a column that was never added.

    ./venv/bin/python tests/smoke_test.py

Exits non-zero if anything fails.
"""
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness                                                # noqa: E402

PAGES = [
    ("Salary",     "/owner/salary?from=2026-09-01&to=2026-09-30"),
    ("Summary",    "/owner/summary?month=2026-09"),
    ("Purchases",  "/owner/purchases?month=2026-09"),
    ("Invoice",    "/owner/invoice?from=2026-09-01&to=2026-09-30"),
    ("Calendar",   "/calendar?month=2026-09"),
    ("Deliveries", "/owner/deliveries?month=2026-09"),
    ("Input",      "/owner"),
    ("Guide",      "/owner/guide"),
    ("Schedule",   "/owner/schedule?month=2026-09"),
    ("Approvals",  "/owner/approvals"),
    ("Recurring",  "/owner/recurring-orders"),
]


def main():
    harness.seed_fixture()
    cl = harness.owner_client()
    failures = []

    print("pages")
    for name, url in PAGES:
        try:
            r = cl.get(url)
            code = r.status_code
        except Exception as exc:                              # noqa: BLE001
            print(f"  FAIL {name:<11} {type(exc).__name__}: {exc}")
            failures.append(name)
            continue
        print(f"  {'ok  ' if code == 200 else 'FAIL'} {name:<11} {code}")
        if code != 200:
            failures.append(name)

    # ---- money checks -----------------------------------------------------
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        sal = harness.appmod._compute_salary("2026-09-01", "2026-09-30")

    print("\nsalary")
    checks = [
        # An assigned purchase run and an ad-hoc request for the same person
        # and date must be paid once, not twice: 1 run + 1 genuine ad-hoc.
        ("procurement deduped", sal["grand_procurement"], 20.0),
        ("production wages",    sal["grand_production"],  410.0),
        ("delivery transport",  sal["grand_delivery"],    12.0),
    ]
    for label, got, want in checks:
        ok = abs(got - want) < 0.005
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<20} {got:>8} (expect {want})")
        if not ok:
            failures.append(label)

    # ---- employee views ---------------------------------------------------
    print("\nemployee views")
    con = harness._con
    # /my-deliveries only shows runs from the last week onwards, so these
    # dates must be relative to today or the check silently rots.
    own_run   = (date.today() + timedelta(days=2)).isoformat()
    other_run = (date.today() + timedelta(days=4)).isoformat()
    con.execute("UPDATE purchase_instances SET date=? WHERE id=2", (own_run,))
    con.execute("INSERT INTO purchase_assignments (purchase_instance_id,"
                "employee_id,completed,created_at) VALUES (2,1,0,'x')")
    con.execute("INSERT INTO purchase_instances (id,date,created_at)"
                " VALUES (3,?,'x')", (other_run,))
    con.execute("INSERT INTO purchase_assignments (purchase_instance_id,"
                "employee_id,completed,created_at) VALUES (3,2,0,'x')")
    con.commit()

    emp = harness.flask_app.test_client()
    with emp.session_transaction() as s:
        s["employee_id"] = 1
        s["employee_name"] = "Yumi"

    r = emp.get("/my-deliveries")
    body = r.get_data(as_text=True)
    ok = (r.status_code == 200 and "strawberry runs" in body.lower()
          and own_run in body and other_run not in body)
    print(f"  {'ok  ' if ok else 'FAIL'} {'my-deliveries lists own runs':<32}")
    if not ok:
        failures.append("my-deliveries runs")

    emp.post("/my-deliveries", data={"action": "purchase_done",
                                     "purchase_id": "2", "completed": "1"},
             follow_redirects=True)
    ok = con.execute("SELECT completed FROM purchase_assignments WHERE"
                     " purchase_instance_id=2 AND employee_id=1"
                     ).fetchone()["completed"] == 1
    print(f"  {'ok  ' if ok else 'FAIL'} {'employee can tick own run':<32}")
    if not ok:
        failures.append("tick own run")

    # An employee must not be able to tick a run assigned to someone else.
    emp.post("/my-deliveries", data={"action": "purchase_done",
                                     "purchase_id": "3", "completed": "1"},
             follow_redirects=True)
    ok = con.execute("SELECT completed FROM purchase_assignments WHERE"
                     " purchase_instance_id=3 AND employee_id=2"
                     ).fetchone()["completed"] == 0
    label = "cannot tick another's run"
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
    if not ok:
        failures.append("cross-employee tick")

    r = emp.get("/calendar?month=2026-09")
    body = r.get_data(as_text=True)
    ok = r.status_code == 200 and "Strawberry run" in body and "Yumi" in body
    print(f"  {'ok  ' if ok else 'FAIL'} {'calendar shows runs':<32}")
    if not ok:
        failures.append("calendar runs")

    # ---- month generation -------------------------------------------------
    # Regression: scheduler used SQLite-only "INSERT OR IGNORE", which
    # Postgres rejects, and read cur.rowcount, which the db wrapper did not
    # expose. Generating a month raised instead of creating shifts.
    print("\ngenerate month")
    r = cl.post("/owner/generate", data={"month": "2026-09"},
                follow_redirects=True)
    n_after = harness._con.execute(
        "SELECT COUNT(*) c FROM shift_instances WHERE date LIKE '2026-09-%'"
    ).fetchone()["c"]
    ok = r.status_code == 200 and n_after == 5      # 5 Wednesdays in Sep 2026
    print(f"  {'ok  ' if ok else 'FAIL'} {'creates the month':<32} "
          f"{n_after} shift date(s)")
    if not ok:
        failures.append("generate month")

    cl.post("/owner/generate", data={"month": "2026-09"}, follow_redirects=True)
    n_twice = harness._con.execute(
        "SELECT COUNT(*) c FROM shift_instances WHERE date LIKE '2026-09-%'"
    ).fetchone()["c"]
    ok = n_twice == n_after
    print(f"  {'ok  ' if ok else 'FAIL'} {'is idempotent':<32} "
          f"{n_twice} after second run")
    if not ok:
        failures.append("generate idempotent")

    # ---- adding a one-off shift day ---------------------------------------
    print("\nadd shift day")

    def n_shifts():
        return con.execute("SELECT COUNT(*) c FROM shift_instances"
                           ).fetchone()["c"]

    before = n_shifts()
    r = cl.post("/owner/shift/add", data={"date": "2026-09-10",
                                          "template_id": "1",
                                          "month": "2026-09",
                                          "back": "calendar"})
    ok = (n_shifts() == before + 1
          and "/calendar" in (r.headers.get("Location") or ""))
    print(f"  {'ok  ' if ok else 'FAIL'} {'adds and returns to calendar':<32}")
    if not ok:
        failures.append("add shift day")

    # Same date twice must not create a duplicate.
    cl.post("/owner/shift/add", data={"date": "2026-09-10",
                                      "template_id": "1", "month": "2026-09"})
    ok = n_shifts() == before + 1
    print(f"  {'ok  ' if ok else 'FAIL'} {'refuses a duplicate':<32}")
    if not ok:
        failures.append("duplicate shift day")

    # A bad template id must not create anything.
    cl.post("/owner/shift/add", data={"date": "2026-09-11",
                                      "template_id": "999", "month": "2026-09"})
    ok = n_shifts() == before + 1
    print(f"  {'ok  ' if ok else 'FAIL'} {'rejects unknown template':<32}")
    if not ok:
        failures.append("bad template")

    # ---- order notes reach the calendar -----------------------------------
    print("\norder notes")
    con.execute("UPDATE orders SET note='取りに来る 9時' WHERE client_id=1")
    con.commit()
    body = cl.get("/calendar?month=2026-09").get_data(as_text=True)
    checks = [
        ("note text on calendar", "取りに来る 9時" in body),
        ("note styled",           'class="ot-note"' in body),
        ("wraps on mobile",       ".ot-note{max-width:100%" in body),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- a pop-up client is offered as pop-up ------------------------------
    print("\npop-up clients")
    con.execute("UPDATE clients SET default_pickup=1 WHERE name='Shoji'")
    con.commit()
    form = cl.get("/owner/orders/2026-09-05").get_data(as_text=True)
    shoji = con.execute("SELECT id FROM clients WHERE name='Shoji'").fetchone()
    ok = f"popupClients  = [{shoji['id']}]" in form.replace("  ", "  ")
    if not ok:                      # tolerate whitespace differences
        import re as _re
        m = _re.search(r"const popupClients\s*=\s*\[([^\]]*)\]", form)
        ok = bool(m) and str(shoji["id"]) in m.group(1)
    print(f"  {'ok  ' if ok else 'FAIL'} {'order form pre-ticks pop-up':<32}")
    if not ok:
        failures.append("popup client default")

    # ---- availability needs no approval -----------------------------------
    print("\navailability")
    emp2 = harness.flask_app.test_client()
    with emp2.session_transaction() as s:
        s["employee_id"] = 2
        s["employee_name"] = "Saku"
    emp2.post("/availability?month=2026-09",
              data={"month": "2026-09", "work_1": "1",
                    "start_1": "06:45", "end_1": "09:30"},
              follow_redirects=True)
    row = con.execute("SELECT status FROM availability WHERE employee_id=2"
                      " AND shift_instance_id=1").fetchone()
    ok = row is not None and row["status"] == "approved"
    print(f"  {'ok  ' if ok else 'FAIL'} {'auto-approved on submit':<32}")
    if not ok:
        failures.append("availability auto-approve")

    # ---- retiring a client -------------------------------------------------
    print("\nremove client")
    con.execute("INSERT INTO clients (id,name,active) VALUES (9,'NeverUsed',1)")
    con.commit()
    cl.post("/owner/recurring-orders/client/9/remove", data={"mode": "keep"})
    ok = con.execute("SELECT COUNT(*) n FROM clients WHERE id=9"
                     ).fetchone()["n"] == 0
    print(f"  {'ok  ' if ok else 'FAIL'} {'deletes an unused client':<32}")
    if not ok:
        failures.append("delete unused client")

    # A client with history must be kept so closed months still add up.
    n_before = con.execute("SELECT COUNT(*) n FROM orders WHERE client_id=1"
                           ).fetchone()["n"]
    cl.post("/owner/recurring-orders/client/1/remove", data={"mode": "keep"})
    kept = con.execute("SELECT active FROM clients WHERE id=1").fetchone()
    n_after = con.execute("SELECT COUNT(*) n FROM orders WHERE client_id=1"
                          ).fetchone()["n"]
    ok = kept is not None and kept["active"] == 0 and n_after == n_before
    print(f"  {'ok  ' if ok else 'FAIL'} {'retires one with orders':<32}")
    if not ok:
        failures.append("retire client with orders")

    # ---- understaffed shifts are surfaced ---------------------------------
    # Generating the month above created Wednesdays with nobody on them, so
    # there are already understaffed days to surface.
    print("\nshifts needing people")
    body = cl.get("/owner/schedule?month=2026-09").get_data(as_text=True)
    checks = [
        ("banner counts short days", "need people" in body),
        ("names the empty day",      "with nobody at all" in body),
        ("row says nobody assigned", "nobody assigned" in body),
        ("short rows tinted",        'class="row-short"' in body),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- per-day shift finish time ----------------------------------------
    print("\nshift finish time")

    def inst_end():
        return con.execute("SELECT end_time FROM shift_instances WHERE id=1"
                           ).fetchone()["end_time"]

    cl.post("/owner/schedule/1", data={"action": "set_end_time",
                                       "end_time": "08:30"})
    ends = [r["end_time"] for r in con.execute(
        "SELECT end_time FROM assignments WHERE shift_instance_id=1")]
    ok = inst_end() == "08:30" and all(e == "08:30" for e in ends)
    print(f"  {'ok  ' if ok else 'FAIL'} {'sets it and trims assignments':<32}")
    if not ok:
        failures.append("shift end time")

    # A finish before the start would make the shift negative-length.
    cl.post("/owner/schedule/1", data={"action": "set_end_time",
                                       "end_time": "05:00"})
    ok = inst_end() == "08:30"
    print(f"  {'ok  ' if ok else 'FAIL'} {'rejects an end before start':<32}")
    if not ok:
        failures.append("shift end validation")

    # Blank clears the override and the template time applies again.
    cl.post("/owner/schedule/1", data={"action": "set_end_time", "end_time": ""})
    ok = inst_end() is None
    print(f"  {'ok  ' if ok else 'FAIL'} {'blank resets to the template':<32}")
    if not ok:
        failures.append("shift end reset")

    # ---- whole-day delivery assignment ------------------------------------
    print("\nday deliverer")
    con.execute("INSERT INTO orders (client_id,date,delivery_date,is_pickup,"
                "qty_original,created_at)"
                " VALUES (2,'2026-09-02','2026-09-02',1,40,'x')")
    con.commit()
    cl.post("/owner/schedule/deliverer", data={"date": "2026-09-02",
                                               "deliverer": "Saku",
                                               "month": "2026-09"})
    ok = all(r["deliverer"] == "Saku" for r in con.execute(
        "SELECT deliverer FROM orders WHERE COALESCE(delivery_date,date)"
        "='2026-09-02' AND (is_pickup IS NULL OR is_pickup=0)"))
    print(f"  {'ok  ' if ok else 'FAIL'} {'assigns every stop that day':<32}")
    if not ok:
        failures.append("day deliverer")

    # Pop-ups are not delivered, so they must never get a driver.
    pop = con.execute("SELECT deliverer FROM orders WHERE is_pickup=1 AND"
                      " COALESCE(delivery_date,date)='2026-09-02'").fetchone()
    ok = not (pop["deliverer"] or "")
    print(f"  {'ok  ' if ok else 'FAIL'} {'skips pop-up orders':<32}")
    if not ok:
        failures.append("day deliverer pop-up")

    # ---- zero-piece orders need no delivery -------------------------------
    # A recurring order left at 0 pcs still creates an order row. It must not
    # show up as a stop, and its driver must not be paid transport for it.
    print("\nzero-piece orders")
    con.execute("INSERT INTO clients (id,name,active) VALUES (11,'Nothing Due',1)")
    con.execute("INSERT INTO orders (client_id,date,delivery_date,deliverer,"
                "created_at) VALUES (11,'2026-09-22','2026-09-22','Saku','x')")
    con.commit()
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        sal_zero = harness.appmod._compute_salary("2026-09-01", "2026-09-30")
        due_days = harness.appmod.production.deliveries_by_date_map(g.db, "2026-09")
        due_stops = harness.appmod.production.deliveries_for_date(g.db, "2026-09-22")
    month_page = cl.get("/owner/deliveries?month=2026-09").get_data(as_text=True)
    zero_checks = [
        ("not a delivery day",      "2026-09-22" not in due_days and not due_stops),
        ("not on deliveries page",  "Nothing Due" not in month_page),
        ("no transport pay",        abs(sal_zero["grand_delivery"]
                                        - sal["grand_delivery"]) < 0.005),
    ]
    for label, ok in zero_checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(f"zero-piece {label}")

    # ---- samples earn nothing ---------------------------------------------
    print("\nsamples")
    import re as _re

    def sales_total():
        body = cl.get("/owner/summary?month=2026-09").get_data(as_text=True)
        m = _re.search(r'sum-label">Sales</span>\s*<span class="sum-value">'
                       r'\$([\d,\.]+)', body)
        return float(m.group(1).replace(",", "")), body

    base, _ = sales_total()
    con.execute("INSERT INTO orders (client_id,date,delivery_date,qty_original,"
                "note,created_at)"
                " VALUES (1,'2026-09-09','2026-09-09',100,'Sample for a cafe','x')")
    con.execute("INSERT INTO orders (client_id,date,delivery_date,is_pickup,"
                "qty_original,note,created_at)"
                " VALUES (2,'2026-09-11','2026-09-11',1,80,'samples','x')")
    con.commit()
    after, body = sales_total()
    ok = abs(after - base) < 0.005 and "given away as samples" in body
    print(f"  {'ok  ' if ok else 'FAIL'} {'noted samples add no revenue':<32}")
    if not ok:
        failures.append("sample revenue")

    # A normal order on the same client must still be charged.
    con.execute("INSERT INTO orders (client_id,date,delivery_date,qty_original,"
                "note,created_at)"
                " VALUES (1,'2026-09-16','2026-09-16',100,'','x')")
    con.commit()
    ok = abs(sales_total()[0] - (base + 520.0)) < 0.005
    print(f"  {'ok  ' if ok else 'FAIL'} {'ordinary orders still charged':<32}")
    if not ok:
        failures.append("non-sample revenue")

    # ---- pick-up as its own channel ---------------------------------------
    print("\npick-up channel")
    con.execute("INSERT INTO clients (id,name,active) VALUES (9,'Grace',1)")
    con.execute("INSERT INTO orders (client_id,date,delivery_date,is_pickup,"
                "pickup_kind,qty_original,created_at)"
                " VALUES (9,'2026-09-09','2026-09-09',1,'popup',100,'x')")
    con.commit()
    as_popup = sales_total()[0]
    con.execute("UPDATE orders SET pickup_kind='pickup' WHERE client_id=9")
    con.commit()
    as_pickup, body = sales_total()
    # 100 x 7.20 less 10% waste = 648; 100 x 6.50 pick-up = 650
    ok = abs((as_pickup - as_popup) - 2.00) < 0.005
    print(f"  {'ok  ' if ok else 'FAIL'} {'priced at the pick-up rate':<32}")
    if not ok:
        failures.append("pick-up rate")

    # A pick-up needs no driver, so it must not reach the delivery queries.
    needing = {r["name"] for r in con.execute(
        """SELECT c.name FROM orders o JOIN clients c ON c.id=o.client_id
            WHERE COALESCE(o.delivery_date,o.date) LIKE '2026-09-%'
              AND (o.is_pickup IS NULL OR o.is_pickup=0)""")}
    ok = "Grace" not in needing
    print(f"  {'ok  ' if ok else 'FAIL'} {'never asks for a driver':<32}")
    if not ok:
        failures.append("pick-up driver")

    # Saving one through the order form.
    cl.post("/owner/orders/2026-09-09", data={
        "action": "add", "client_id": "9", "channel": "pickup",
        "qty_original": "40", "delivery_date": "2026-09-09"},
        follow_redirects=True)
    row = con.execute("SELECT is_pickup, pickup_kind FROM orders"
                      " WHERE client_id=9 AND qty_original=40").fetchone()
    ok = row is not None and row["is_pickup"] == 1 and row["pickup_kind"] == "pickup"
    print(f"  {'ok  ' if ok else 'FAIL'} {'order form saves the channel':<32}")
    if not ok:
        failures.append("pick-up form")

    # ---- flat monthly amount ----------------------------------------------
    print("\nflat monthly amount")
    before = sales_total()[0]
    con.execute("INSERT INTO clients (id,name,active,unit_price)"
                " VALUES (10,'W San Francisco',1,5.00)")
    con.execute("INSERT INTO orders (client_id,date,delivery_date,qty_original,"
                "created_at) VALUES (10,'2026-09-10','2026-09-10',300,'x')")
    con.execute("INSERT INTO consignment_sales (client_id,month,amount)"
                " VALUES (10,'2026-09',500)")
    con.commit()
    after, body = sales_total()
    # 300 pcs would be $1500 per-piece; the flat figure replaces it.
    ok = abs((after - before) - 500.0) < 0.005 and "flat amount entered" in body
    print(f"  {'ok  ' if ok else 'FAIL'} {'replaces the per-piece total':<32}")
    if not ok:
        failures.append("flat amount")

    # ---- cost-spreadsheet parser -----------------------------------------
    print("\ncost sheet parser")
    parse = harness.appmod.parse_cost_sheet
    cases = [
        # (name, csv, expected row count, expected first month)
        ("iso dates",
         "Date,Item,Amount\n2026-08-03,Mixer bowl,84.50\n", 1, "2026-08"),
        ("us dates, $ and commas",
         'Date,Description,Cost\n8/3/2026,Uber,"$1,284.50"\n', 1, "2026-08"),
        ("month names, TOTAL row skipped",
         "Month,Expense,Amount\nAugust 2026,Scale,120\n,TOTAL,120\n", 1, "2026-08"),
        ("negative becomes revenue",
         "Date,Item,Amount\n2026-08-01,Refund,-50\n", 1, "2026-08"),
    ]
    for name, csv_text, want_n, want_month in cases:
        rows, _ = parse(csv_text)
        ok = len(rows) == want_n and rows and rows[0]["month"] == want_month
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<32} {len(rows)} row(s)")
        if not ok:
            failures.append(name)
    neg, _ = parse("Date,Item,Amount\n2026-08-01,Refund,-50\n")
    ok = neg and neg[0]["kind"] == "revenue"
    print(f"  {'ok  ' if ok else 'FAIL'} {'negative -> revenue':<32}")
    if not ok:
        failures.append("negative -> revenue")
    bad, problems = parse("Date,Item,Notes\n2026-08-01,x,y\n")
    ok = not bad and problems
    print(f"  {'ok  ' if ok else 'FAIL'} {'missing amount column reports':<32}")
    if not ok:
        failures.append("missing amount column")

    # ---- shift report approval preview -----------------------------------
    # The owner must see the manager's reported hours and the pay they imply
    # before approving. Yumi (manager) reports 06:30–10:00, Saku 06:45–09:30.
    print("\nshift report preview")
    con.execute("INSERT INTO shift_reports (id,shift_instance_id,submitted_by,"
                "status,submitted_at) VALUES (90,1,1,'pending','x')")
    con.execute("INSERT INTO shift_report_hours (report_id,employee_id,"
                "actual_start,actual_end) VALUES (90,1,'06:30','10:00')")
    con.execute("INSERT INTO shift_report_hours (report_id,employee_id,"
                "actual_start,actual_end) VALUES (90,2,'06:45','09:30')")
    con.commit()
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        pv = harness.appmod._shift_report_preview(90, 1, "2026-09-02")
        pieces = harness.appmod.production.day_totals(g.db, "2026-09-02")["total"]
        pr = harness.appmod.piece_rate()
    ph = 3.5 + 2.75
    rate = pr * pieces / ph
    yumi = next(p for p in pv["people"] if p["name"] == "Yumi")
    body = cl.get("/owner/approvals").get_data(as_text=True)
    checks = [
        ("day has pieces",        pieces > 0),
        ("reported hours used",   abs(pv["person_hours"] - ph) < 0.005),
        ("manager pay +5%",       abs(yumi["pay"] - 3.5 * rate * 1.05) < 0.005),
        ("pool fully paid out",   abs(pv["total_pay"] - pr * pieces
                                      - 3.5 * rate * 0.05) < 0.005),
        ("finish shown",          pv["finish"] == "10:00" and "10:00" in body),
        ("page shows reported",   "06:30–10:00" in body),
        ("page shows pay",        f"${yumi['pay']:.2f}" in body),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- chief manager ---------------------------------------------------
    # Selectable only on shifts from 2026-10-01. No +5%: instead paid from
    # 15 minutes before the scheduled start.
    print("\nchief manager")
    con.execute("INSERT INTO shift_instances (id,template_id,date)"
                " VALUES (2,1,'2026-10-07')")
    con.commit()

    def save_roles(instance_id, yumi_role):
        cl.post(f"/owner/schedule/{instance_id}", data={
            "action": "save", "include": ["1", "2"],
            "role_1": str(yumi_role), "role_2": "0"})
        return {r["employee_id"]: r["is_manager"] for r in con.execute(
            "SELECT employee_id, is_manager FROM assignments"
            " WHERE shift_instance_id=?", (instance_id,)).fetchall()}

    sept_page = cl.get("/owner/schedule/1").get_data(as_text=True)
    oct_page  = cl.get("/owner/schedule/2").get_data(as_text=True)
    oct_roles  = save_roles(2, 2)
    sept_roles = save_roles(1, 2)
    save_roles(1, 1)
    con.execute("UPDATE assignments SET is_manager=2"
                " WHERE shift_instance_id=1 AND employee_id=1")
    con.commit()
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        pv = harness.appmod._shift_report_preview(90, 1, "2026-09-02")
    chief = next(p for p in pv["people"] if p["name"] == "Yumi")
    # Only a manager, not a chief manager, may submit the shift report.
    chief_report = emp.get("/my-shift/1/report")
    con.execute("UPDATE assignments SET is_manager=1"
                " WHERE shift_instance_id=1 AND employee_id=1")
    con.commit()
    mgr_report = emp.get("/my-shift/1/report")
    # Weekly default chief manager (Wednesday = Yumi). Saving a shift by hand
    # keeps the owner's choice even when it differs from the default.
    cl.post("/owner/settings/weekday-managers",
            data={"mgr_2": "", "chief_2": "1"})
    input_page = cl.get("/owner").get_data(as_text=True)
    wk_oct  = save_roles(2, 0)
    wk_sept = save_roles(1, 0)
    save_roles(2, 2)
    cl.post("/owner/settings/weekday-managers", data={})
    wm_left = con.execute("SELECT COUNT(*) FROM weekday_managers").fetchone()[0]
    save_roles(1, 1)
    # Unreported Oct shift, Yumi chief 06:45–09:30, Saku crew 06:45–09:30:
    # Yumi counts from 06:30, so 3.0 h of 5.75 person-hours in the pool.
    con.execute("INSERT INTO orders (client_id,date,delivery_date,qty_original,"
                "created_at) VALUES (1,'2026-10-07','2026-10-07',100,'x')")
    con.commit()
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        sal = harness.appmod._compute_salary("2026-10-07", "2026-10-07")
        oct_pieces = harness.appmod.production.day_totals(
            g.db, "2026-10-07")["total"]
    oct_shift = {e["name"]: e["shifts"][0] for e in sal["employees"]}
    oct_rate = pr * oct_pieces / 5.75
    checks = [
        ("weekly chief on Input",     'name="chief_2"' in input_page),
        ("manual role beats default", wk_oct == {1: 0, 2: 0}),
        ("manual pre-Oct kept too",   wk_sept == {1: 0, 2: 0}),
        ("weekly defaults clear",     wm_left == 0),
        ("option hidden before Oct",  ">Chief manager ⭐<" not in sept_page),
        ("option shown from Oct",     ">Chief manager ⭐<" in oct_page),
        ("Oct shift saves chief",     oct_roles == {1: 2, 2: 0}),
        ("Sept shift clamps to mgr",  sept_roles == {1: 1, 2: 0}),
        ("chief reported: no +5%",    abs(chief["pay"] - 3.5 * rate) < 0.005),
        ("chief pool fully paid out", abs(pv["total_pay"] - pr * pieces) < 0.005),
        ("chief counts 15 min early", oct_shift["Yumi"]["hours"] == 3.0
                                      and oct_shift["Yumi"]["time_window"]
                                      == "06:30–09:30"),
        ("chief early pay, no +5%",   oct_pieces > 0 and abs(
                                      oct_shift["Yumi"]["pay"] - 3.0 * oct_rate)
                                      < 0.005),
        ("crew share of pool",        abs(oct_shift["Saku"]["pay"]
                                          - 2.75 * oct_rate) < 0.005),
        ("chief cannot file report",  chief_report.status_code == 302),
        ("manager can file report",   mgr_report.status_code == 200),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- piece rate by date ----------------------------------------------
    # $2.10 base, $2.20 for shifts from 2026-09-19 (as seeded by migrate_db).
    # Pay is computed live, so the 9/2 shift keeps 2.10 even though it is
    # approved, and the 10/7 shift picks up 2.20.
    print("\npiece rate by date")
    con.execute("UPDATE settings SET value='2.10' WHERE key='piece_rate'")
    con.commit()
    cl.post("/owner/settings/piece-rate",
            data={"piece_rate": "2.20", "start_date": "2026-09-19"})
    con.execute("UPDATE assignments SET actual_start='06:45', actual_end='09:30'"
                " WHERE shift_instance_id=1")
    con.commit()
    with harness.flask_app.test_request_context():
        from flask import g
        g.db = harness.stub.get_db()
        am = harness.appmod
        rates = (am.piece_rate("2026-09-18"), am.piece_rate("2026-09-19"),
                 am.piece_rate("2026-10-07"))
        sep = am._compute_salary("2026-09-02", "2026-09-02")
        oct_ = am._compute_salary("2026-10-07", "2026-10-07")
        sep_pieces = am.production.day_totals(g.db, "2026-09-02")["total"]
    sep_rate = next(e for e in sep["employees"] if e["name"] == "Saku")["shifts"][0]["rate"]
    oct_saku = next(e for e in oct_["employees"] if e["name"] == "Saku")["shifts"][0]
    salary_page = cl.get("/owner/salary?from=2026-09-01&to=2026-10-31")
    salary_body = salary_page.get_data(as_text=True)
    input_body = cl.get("/owner").get_data(as_text=True)
    checks = [
        ("2.10 before 9/19",          rates[0] == 2.10),
        ("2.20 from 9/19",            rates[1] == 2.20 and rates[2] == 2.20),
        ("approved Sept shift 2.10",  abs(sep_rate - round(2.10 * sep_pieces / 5.5, 2))
                                      < 0.005),
        ("Oct hourly rate at 2.20",   abs(oct_saku["rate"] - round(2.20 * oct_pieces / 5.75, 2))
                                      < 0.005
                                      and abs(oct_saku["pay"] - 2.75 * 2.20 * oct_pieces / 5.75)
                                      < 0.005),
        ("salary shows both rates",   salary_page.status_code == 200
                                      and "$2.10 before 2026-09-19" in salary_body
                                      and "$2.20 from 2026-09-19" in salary_body),
        ("Input shows rate history",  "$2.20 from 2026-09-19" in input_body),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- weekly defaults applied automatically --------------------------
    # From 2026-10-01 the Wednesday defaults (Saku manager, Yumi chief) are
    # stamped onto shifts they already work. Nobody is added to a shift, no
    # role is lowered, and a shift with its own chief keeps it.
    print("\nweekly defaults auto-applied")
    con.execute("INSERT INTO weekday_managers (weekday,employee_id,"
                "chief_employee_id) VALUES (2,2,1)")
    con.execute("INSERT INTO shift_templates (id,weekday,label,start_time,"
                "end_time,quantity,min_people,max_people)"
                " VALUES (9,2,'Wed test','06:45','09:30',80,2,4)")
    ids = {}
    for key, d in [(31, "2026-10-14"), (32, "2026-10-21"), (33, "2026-09-23"),
                   (34, "2026-10-28"), (35, "2026-11-04"), (36, "2026-12-02"),
                   (37, "2026-12-09"), (38, "2026-12-16")]:
        ids[key] = con.execute("INSERT INTO shift_instances (template_id,date)"
                               " VALUES (9,?)", (d,)).lastrowid
    for iid, eid, role in [(31, 1, 0), (31, 2, 0), (32, 2, 0), (33, 1, 0),
                           (33, 2, 0), (34, 1, 0), (34, 2, 2)]:
        con.execute("INSERT INTO assignments (shift_instance_id,employee_id,"
                    "start_time,end_time,is_manager)"
                    " VALUES (?,?,'06:45','09:30',?)", (ids[iid], eid, role))
    for eid in (1, 2):
        for key in (35, 36, 38):
            con.execute("INSERT INTO availability (employee_id,shift_instance_id,"
                        "start_time,end_time,status,submitted_at)"
                        " VALUES (?,?,'06:45','09:30','approved','x')",
                        (eid, ids[key]))
    # 37: owner swapped roles by hand, Saku chief and Yumi manager.
    for eid, role in [(1, 1), (2, 2)]:
        con.execute("INSERT INTO assignments (shift_instance_id,employee_id,"
                    "start_time,end_time,is_manager)"
                    " VALUES (?,?,'06:45','09:30',?)", (ids[37], eid, role))
    con.execute("DELETE FROM settings WHERE key='weekday_roles_backfill_2026_10'")
    con.commit()
    harness.appmod.backfill_weekday_default_roles()
    cl.post("/owner/schedule/auto-month", data={"month": "2026-11", "mode": "empty"})

    def roles_of(iid):
        return {r["employee_id"]: r["is_manager"] for r in con.execute(
            "SELECT employee_id, is_manager FROM assignments"
            " WHERE shift_instance_id=?", (ids[iid],)).fetchall()}

    def preselected(iid, eid):
        page = cl.get(f"/owner/schedule/{ids[iid]}").get_data(as_text=True)
        m = re.search(rf'name="role_{eid}".*?</select>', page, re.S)
        sel = m and re.search(r'value="(\d)" selected', m.group(0))
        return int(sel.group(1)) if sel else 0

    def default_offered(iid, eid):
        page = cl.get(f"/owner/schedule/{ids[iid]}").get_data(as_text=True)
        m = re.search(rf'name="role_{eid}"[^>]*data-default-role="(\d)"', page)
        return int(m.group(1)) if m else None

    # Nobody is on 36 yet: no row may show a role, but ticking one in offers
    # the weekly default.
    form_defaults = (preselected(36, 1), preselected(36, 2),
                     default_offered(36, 1), default_offered(36, 2))
    # Owner makes Saku the only manager on 36 by hand: Yumi (default chief)
    # is saved as crew and stays crew through later automatic passes.
    cl.post(f"/owner/schedule/{ids[36]}", data={
        "action": "save", "include": ["1", "2"], "role_1": "0", "role_2": "1"})
    # 38: Yumi (default chief) is available but not working. Leaving her out
    # and clearing her role must stick: her row may not read as chief.
    cl.post(f"/owner/schedule/{ids[38]}", data={
        "action": "save", "include": ["2"], "role_1": "0", "role_2": "1"})
    harness.appmod.backfill_weekday_default_roles()
    cl.post("/owner/schedule/auto-month", data={"month": "2026-12", "mode": "empty"})
    with harness.flask_app.test_request_context():
        harness.appmod.apply_weekday_default_roles(harness.stub.get_db())

    checks = [
        ("form offers defaults",      form_defaults == (0, 0, 2, 1)),
        ("absent default not chief",  roles_of(38) == {2: 1}
                                      and preselected(38, 1) == 0),
        ("manual save survives auto", roles_of(36) == {1: 0, 2: 1}),
        ("swapped roles left alone",  roles_of(37) == {1: 1, 2: 2}),
        ("Oct backfill chief + mgr",  roles_of(31) == {1: 2, 2: 1}),
        ("absent chief not added",    roles_of(32) == {2: 1}),
        ("Sept shifts untouched",     roles_of(33) == {1: 0, 2: 0}),
        ("existing chief kept",       roles_of(34) == {1: 0, 2: 2}),
        ("auto-month applies roles",  roles_of(35) == {1: 2, 2: 1}),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(label)

    # ---- recurring apply leaves made days alone ---------------------------
    # Applying recurring orders to the current month must not touch days
    # before today: those pieces are already made. A later month syncs fully.
    print("\nrecurring apply / sync")
    con.execute("INSERT INTO clients (id,name,active) VALUES (21,'Rec Cafe',1)")
    con.execute("INSERT INTO recurring_orders (client_id,weekday,qty_original)"
                " VALUES (21,2,40)")
    for d in ["2027-02-03", "2027-02-10", "2027-02-17", "2027-02-24",
              "2027-03-03"]:
        con.execute("INSERT INTO shift_instances (template_id,date)"
                    " VALUES (1,?)", (d,))
    for d in ["2027-02-03", "2027-02-17"]:
        con.execute("INSERT INTO orders (client_id,date,delivery_date,"
                    "qty_original,created_at) VALUES (21,?,?,7,'x')", (d, d))
    con.commit()

    real_date = harness.appmod.date

    class _FrozenDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2027, 2, 10)

    harness.appmod.date = _FrozenDate
    try:
        cl.post("/owner/recurring-orders/apply", data={"month": "2027-02"})
        cl.post("/owner/recurring-orders/apply", data={"month": "2027-03"})
    finally:
        harness.appmod.date = real_date
    rec_qty = {r["date"]: r["qty_original"] for r in con.execute(
        "SELECT date, qty_original FROM orders WHERE client_id=21").fetchall()}
    checks = [
        ("past day qty untouched",   rec_qty.get("2027-02-03") == 7),
        ("today gets an order",      rec_qty.get("2027-02-10") == 40),
        ("later day synced",         rec_qty.get("2027-02-17") == 40),
        ("later day added",          rec_qty.get("2027-02-24") == 40),
        ("next month synced fully",  rec_qty.get("2027-03-03") == 40),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(f"recurring {label}")

    # ---- removing recurring defaults asks about upcoming orders -----------
    # Removing a recurring default first shows the client's orders from today
    # onward on that weekday. The owner keeps them or deletes them; orders
    # before today are never touched either way.
    print("\nrecurring delete confirm")
    con.execute("INSERT INTO clients (id,name,active) VALUES (22,'Mon Bakery',1)")
    con.execute("INSERT INTO clients (id,name,active) VALUES (23,'Gone Cafe',1)")
    for cid in (22, 23):
        for wd in (0, 1):
            con.execute("INSERT INTO recurring_orders (client_id,weekday,"
                        "qty_original) VALUES (?,?,60)", (cid, wd))
    # 2027-04-05 Mon (past), 04-12 Mon, 04-13 Tue, 04-19 Mon, 04-21 Wed
    for cid in (22, 23):
        for d, q in [("2027-04-05", 11), ("2027-04-12", 12), ("2027-04-13", 13),
                     ("2027-04-19", 14), ("2027-04-21", 15)]:
            con.execute("INSERT INTO orders (client_id,date,delivery_date,"
                        "qty_original,created_at) VALUES (?,?,?,?,'x')",
                        (cid, d, d, q))
    con.commit()

    def order_dates(cid):
        return sorted(r["date"] for r in con.execute(
            "SELECT date FROM orders WHERE client_id=?", (cid,)).fetchall())

    def rec_days(cid):
        return sorted(r["weekday"] for r in con.execute(
            "SELECT weekday FROM recurring_orders WHERE client_id=?",
            (cid,)).fetchall())

    def form_ids(page):
        return _re.findall(r'name="order_id" value="(\d+)"', page)

    all_dates = ["2027-04-05", "2027-04-12", "2027-04-13", "2027-04-19",
                 "2027-04-21"]
    real_date = harness.appmod.date

    class _AprilDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2027, 4, 12)

    harness.appmod.date = _AprilDate
    try:
        # one weekday: the page lists only upcoming Mondays
        r1 = cl.get("/owner/recurring-orders/delete?client_id=22&weekday=0")
        p1 = r1.get_data(as_text=True)
        page_ok = (r1.status_code == 200 and "2027-04-12" in p1
                   and "2027-04-19" in p1 and "2027-04-05" not in p1
                   and "2027-04-13" not in p1 and len(form_ids(p1)) == 2
                   and "Remove recurring setting only" in p1)
        unchanged_by_get = rec_days(22) == [0, 1] and order_dates(22) == all_dates
        # a POST with no choice must not delete anything
        cl.post("/owner/recurring-orders/delete",
                data={"client_id": 22, "weekday": 0})
        no_choice_safe = rec_days(22) == [0, 1] and order_dates(22) == all_dates
        cl.post("/owner/recurring-orders/delete",
                data={"client_id": 22, "weekday": 1, "mode": "keep"})
        keep_ok = rec_days(22) == [0] and order_dates(22) == all_dates
        ids = form_ids(p1)
        cl.post("/owner/recurring-orders/delete",
                data={"client_id": 22, "weekday": 0, "mode": "orders",
                      "order_id": ids})
        orders_ok = (rec_days(22) == [] and order_dates(22)
                     == ["2027-04-05", "2027-04-13", "2027-04-21"])

        # whole client: lists Mon + Tue upcoming, never the past Monday
        p2 = cl.get("/owner/recurring-orders/client/23/remove").get_data(as_text=True)
        client_page_ok = (len(form_ids(p2)) == 3 and "2027-04-05" not in p2
                          and "2027-04-21" not in p2)
        cl.post("/owner/recurring-orders/client/23/remove",
                data={"mode": "orders", "order_id": form_ids(p2)})
        kept = con.execute("SELECT active FROM clients WHERE id=23").fetchone()
        client_ok = (rec_days(23) == [] and kept is not None
                     and kept["active"] == 0
                     and order_dates(23) == ["2027-04-05", "2027-04-21"])
    finally:
        harness.appmod.date = real_date
    checks = [
        ("page lists upcoming day",   page_ok),
        ("viewing changes nothing",   unchanged_by_get),
        ("no choice deletes nothing", no_choice_safe),
        ("keep leaves orders",        keep_ok),
        ("delete spares past/others", orders_ok),
        ("client page lists its days", client_page_ok),
        ("client delete spares past", client_ok),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(f"recurring delete {label}")

    # ---- zero-piece orders are not shown or created ------------------------
    # An order of 0 pieces has nothing to make. The calendars and the owner's
    # schedule leave it out (the order-entry page still lists it), Apply does
    # not create one, and the one-time cleanup deletes upcoming ones that have
    # no note — never one dated before today.
    print("\nzero-piece orders hidden")
    con.execute("INSERT INTO clients (id,name,active) VALUES (24,'Zero Tea',1)")
    con.execute("INSERT INTO clients (id,name,active) VALUES (25,'Real Cafe',1)")
    zero_inst = con.execute("INSERT INTO shift_instances (template_id,date)"
                            " VALUES (1,'2027-05-05')").lastrowid
    for cid, q in [(24, 0), (25, 30)]:
        con.execute("INSERT INTO orders (client_id,date,delivery_date,"
                    "qty_original,created_at) VALUES (?,?,?,?,'x')",
                    (cid, "2027-05-05", "2027-05-05", q))
    con.commit()
    shown = {
        "calendar":   cl.get("/calendar?month=2027-05"),
        "dashboard":  cl.get("/owner?month=2027-05"),
        "shift page": cl.get(f"/owner/schedule/{zero_inst}"),
    }
    con.execute("INSERT INTO settings (key,value) VALUES"
                " ('public_calendar_token','zerotok') ON CONFLICT(key) DO NOTHING")
    con.commit()
    token = con.execute("SELECT value FROM settings"
                        " WHERE key='public_calendar_token'").fetchone()["value"]
    shown["public calendar"] = cl.get(f"/public/calendar/{token}?month=2027-05")
    entry = cl.get("/owner/orders/2027-05-05").get_data(as_text=True)
    # The dashboard also lists every client by name, so look at its order
    # lines only there.
    def order_names(key, page):
        if key == "dashboard":
            return " ".join(_re.findall(r'class="order-client">([^<]*)<', page))
        return page
    checks = [(f"{k} hides 0-piece order",
               r.status_code == 200
               and "Real Cafe" in order_names(k, r.get_data(as_text=True))
               and "Zero Tea" not in order_names(k, r.get_data(as_text=True)))
              for k, r in shown.items()]
    checks.append(("order entry still lists it", "Zero Tea" in entry))

    # Apply creates nothing for a 0-piece default (Wednesday = weekday 2)
    con.execute("INSERT INTO recurring_orders (client_id,weekday) VALUES (24,2)")
    con.execute("INSERT INTO shift_instances (template_id,date)"
                " VALUES (1,'2027-06-02')")
    con.commit()
    cl.post("/owner/recurring-orders/apply", data={"month": "2027-06"})
    n_zero_created = con.execute(
        "SELECT COUNT(*) n FROM orders WHERE client_id=24 AND date LIKE ?",
        ("2027-06-%",)).fetchone()["n"]
    checks.append(("apply skips 0-piece default", n_zero_created == 0))

    # One-time cleanup, run from db.py against this database
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "real_db", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "db.py"))
    real_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_db)
    con.execute("INSERT INTO clients (id,name,active) VALUES (26,'Cleanup Co',1)")
    for d, q, note in [("2027-07-01", 0, ""),       # past: keep
                       ("2027-07-08", 0, None),     # today, empty: delete
                       ("2027-07-09", 0, "  "),     # blank note: delete
                       ("2027-07-10", 0, "call first"),  # note: keep
                       ("2027-07-11", 5, ""),       # has pieces: keep
                       ("2027-07-12", 0, "")]:      # pop-up sales: keep
        con.execute("INSERT INTO orders (client_id,date,delivery_date,"
                    "qty_original,note,created_at) VALUES (26,?,?,?,?,'x')",
                    (d, d, q, note))
    sold = con.execute("SELECT id FROM orders WHERE client_id=26"
                       " AND date='2027-07-12'").fetchone()["id"]
    con.execute("INSERT INTO popup_sales (order_id,pcs_sold) VALUES (?,3)",
                (sold,))
    con.commit()
    gone = real_db.delete_upcoming_empty_orders(harness.stub.get_db(),
                                                "2027-07-08")
    con.commit()
    left = sorted(r["date"] for r in con.execute(
        "SELECT date FROM orders WHERE client_id=26").fetchall())
    checks += [
        ("cleanup lists what it deletes",
         [(r["date"], r["client_name"]) for r in gone]
         == [("2027-07-08", "Cleanup Co"), ("2027-07-09", "Cleanup Co")]),
        ("cleanup spares past/note/sales",
         left == ["2027-07-01", "2027-07-10", "2027-07-11", "2027-07-12"]),
        ("cleanup leaves other clients",
         con.execute("SELECT COUNT(*) n FROM orders WHERE client_id=24"
                     " AND date='2027-05-05'").fetchone()["n"] == 1),
    ]
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32}")
        if not ok:
            failures.append(f"zero-piece {label}")

    # ---- startup runs clean -----------------------------------------------
    # Regression: the weekday-roles backfill runs at import and called a helper
    # defined further down the file, so it raised NameError and took every
    # route down — login included. It only bites when weekday defaults exist
    # and a shift is staffed, which no fixture had, so the suite stayed green
    # while production was off. Import in a fresh process with that seeded.
    print("\nstartup")
    import subprocess
    probe = subprocess.run(
        [sys.executable, "-c",
         "import harness, sys;"
         " sys.exit(1 if harness.appmod.STARTUP_ERROR else 0)"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env={**os.environ, "SHIFTO_TEST_STARTUP_WORK": "1"},
        capture_output=True, text=True,
    )
    ok = probe.returncode == 0
    print(f"  {'ok  ' if ok else 'FAIL'} {'imports with no startup error':<32}")
    if not ok:
        failures.append("startup error")
        tail = (probe.stdout + probe.stderr).strip().splitlines()
        for line in tail[-12:]:
            print("        ", line)

    print("\nfailures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
