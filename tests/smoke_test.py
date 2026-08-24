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

    print("\nfailures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
