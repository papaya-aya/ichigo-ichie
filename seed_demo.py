"""Optional: load demo employees + availability so you can try the app immediately.

Run:  ./venv/bin/python seed_demo.py 2026-07
It creates 8 employees (PIN 1234), generates the given month's shifts, and submits
randomly-trimmed availability for each. Approve them on the Approvals page, then
use Auto-assign on the Schedule page. Safe to re-run.
"""
import sys

import db as database
from db import hash_password as generate_password_hash
from scheduler import generate_instances, to_minutes, to_hhmm

DEMO_EMPLOYEES = ["Aya", "Ken", "Mio", "Sora", "Haru", "Nao", "Rin", "Yuki"]
PIN = "1234"


def deterministic_window(shift_start, shift_end, emp_idx, inst_idx):
    """Most people take the full shift; a few take a partial window. No RNG (keeps it reproducible)."""
    s, e = to_minutes(shift_start), to_minutes(shift_end)
    seed = (emp_idx * 7 + inst_idx * 3) % 10
    if seed == 0:      # arrives 30 min late
        s = min(s + 30, e - 15)
    elif seed == 1:    # leaves 45 min early
        e = max(e - 45, s + 15)
    return to_hhmm(s), to_hhmm(e)


def main(month):
    database.init_db()
    conn = database.get_db()

    emp_ids = []
    for name in DEMO_EMPLOYEES:
        row = conn.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row:
            emp_ids.append(row["id"])
            continue
        cur = conn.execute(
            "INSERT INTO employees (name, pin_hash, created_at) VALUES (?, ?, ?)",
            (name, generate_password_hash(PIN), database.now_iso()),
        )
        emp_ids.append(cur.lastrowid)
    conn.commit()

    y, m = int(month[:4]), int(month[5:7])
    generate_instances(conn, y, m)

    instances = conn.execute(
        """SELECT si.id AS instance_id, t.start_time, t.end_time
             FROM shift_instances si JOIN shift_templates t ON t.id = si.template_id
            WHERE si.date LIKE ?""",
        (month + "-%",),
    ).fetchall()

    submitted = 0
    for inst_idx, inst in enumerate(instances):
        for emp_idx, eid in enumerate(emp_ids):
            # Skip ~1 in 4 so not everyone is available every shift.
            if (emp_idx + inst_idx) % 4 == 0:
                continue
            start, end = deterministic_window(
                inst["start_time"], inst["end_time"], emp_idx, inst_idx)
            conn.execute(
                """INSERT INTO availability
                     (employee_id, shift_instance_id, start_time, end_time, status, submitted_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)
                   ON CONFLICT(employee_id, shift_instance_id) DO NOTHING""",
                (eid, inst["instance_id"], start, end, database.now_iso()),
            )
            submitted += 1
    conn.commit()
    conn.close()
    print(f"Seeded {len(DEMO_EMPLOYEES)} employees (PIN {PIN}) and "
          f"{submitted} pending availability rows for {month}.")
    print("Next: log in as owner, approve on /owner/approvals, then auto-assign on /owner/schedule.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-07")
