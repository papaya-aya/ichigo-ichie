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

    print("\nfailures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
