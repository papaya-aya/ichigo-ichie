"""Render every owner page and check the money maths.

Catches the class of bug that only shows up at render time — a url_for with a
None argument, a missing template variable, a column that was never added.

    ./venv/bin/python tests/smoke_test.py

Exits non-zero if anything fails.
"""
import os
import sys

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
    con.execute("INSERT INTO purchase_assignments (purchase_instance_id,"
                "employee_id,completed,created_at) VALUES (2,1,0,'x')")
    con.execute("INSERT INTO purchase_instances (id,date,created_at)"
                " VALUES (3,'2026-09-06','x')")
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
          and "2026-09-04" in body and "2026-09-06" not in body)
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
    cl.post("/owner/recurring-orders/client/9/remove")
    ok = con.execute("SELECT COUNT(*) n FROM clients WHERE id=9"
                     ).fetchone()["n"] == 0
    print(f"  {'ok  ' if ok else 'FAIL'} {'deletes an unused client':<32}")
    if not ok:
        failures.append("delete unused client")

    # A client with history must be kept so closed months still add up.
    n_before = con.execute("SELECT COUNT(*) n FROM orders WHERE client_id=1"
                           ).fetchone()["n"]
    cl.post("/owner/recurring-orders/client/1/remove")
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
    # Weekly default chief manager (Wednesday = Yumi): chief from Oct,
    # manager before.
    cl.post("/owner/settings/weekday-managers",
            data={"mgr_2": "", "chief_2": "1"})
    input_page = cl.get("/owner").get_data(as_text=True)
    wk_oct  = save_roles(2, 0)
    wk_sept = save_roles(1, 0)
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
        ("weekly chief from Oct",     wk_oct == {1: 2, 2: 0}),
        ("weekly chief mgr pre-Oct",  wk_sept == {1: 1, 2: 0}),
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

    print("\nfailures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
