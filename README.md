# 🍓 Ichigo Ichie — Shift Manager

A small shift-management web app for the Ichigo Ichie mochi/daifuku workshop.
Employees submit monthly availability (full or partial windows), the owner approves
them, and an auto-assigner staffs each production shift to keep the average headcount
within the per-shift min/max band. A shared calendar shows the schedule.

Built with **Python (Flask) + SQLite** — no Node, no build step.

## Run it

```bash
./run.sh
```

Then open <http://localhost:5001>. On first run it creates `shifto.db` and seeds the
four weekly shifts. To reach it from a phone on the same Wi-Fi, use your Mac's LAN IP
(e.g. `http://192.168.1.20:5001`).

To run manually instead of `run.sh`:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

## First steps

1. Open the site → **Owner** tab → password `ichigo-admin`. **Change it** on the dashboard.
2. On the dashboard, **add your employees** (each gets a name + initial PIN you set).
3. **Generate a month** (defaults to next month) so its shift dates exist.
4. Employees sign in (Employee tab → name + PIN) and submit availability for that month.
5. You review on **Approvals** and approve the ones you want.
6. On **Schedule**, open a shift and click **Auto-assign**, then tweak by hand if needed.
7. Everyone can open the **Calendar** anytime to see who's on.

## The four weekly shifts (seeded)

| Day | Hours       | Approx qty | Min | Max |
|-----|-------------|-----------:|----:|----:|
| Mon | 06:45–08:30 | 36         | 3   | 3   |
| Tue | 06:45–09:30 | 86         | 4   | 5   |
| Wed | 14:30–17:30 | 85         | 4   | 5   |
| Fri | 06:45–09:30 | 102        | 5   | 7   |

Change these later in the `shift_templates` table (or ask to add an editor screen).

## Production / order tracking

The app also tracks client orders, merging what used to live in the production
spreadsheet. On the **Orders** page (owner-only) you enter each client's order per
production day, broken down by flavor (Original / Matcha / Hojicha / Other).

Orders and staffing are linked by **productivity = pieces ÷ person-hours**:

- The app computes *actual* productivity from each person's assigned window.
- It suggests a **team size** from order volume:
  `pieces ÷ target_productivity ÷ shift_length`, clamped to the shift's min/max band.
- The schedule and each shift page flag when assigned labor falls **short for the
  day's orders**, so you can add a person or trim orders.

Set **target productivity** (pieces per person-hour, default 6.5) and manage
**clients** on the owner dashboard.

## How assignment handles partial shifts

Because someone might only come for, say, the first hour, headcount changes over the
shift. The app slices each shift into 15-minute slots, counts how many people are
present in each, and the auto-assigner greedily adds people to bring every slot up to
the minimum without pushing any slot over the maximum. The **coverage timeline** on a
shift page shows this minute-by-minute (amber = under, red = over). Keeping every slot
in band guarantees the time-weighted **average** is in band too.

## Try it with demo data (optional)

```bash
./venv/bin/python seed_demo.py 2026-07
```

Adds 8 demo employees (PIN `1234`) with sample availability for July 2026 so you can
exercise approvals → auto-assign → calendar end to end.

## Deploying later

It's a standard Flask app. To put it online (so employees use it from anywhere):

- Set a real secret: `export SHIFTO_SECRET=$(openssl rand -hex 16)`.
- Serve with a production server, e.g. `gunicorn 'app:app'` (add `gunicorn` to
  requirements), behind a host like Render or Railway (free tiers).
- `shifto.db` is a single file — back it up, or move to managed Postgres later.

## Files

- `app.py` — routes (auth, availability, approvals, schedule, calendar)
- `scheduler.py` — month generation, coverage analysis, auto-assign algorithm
- `db.py` / `schema.sql` — SQLite setup + seed
- `templates/`, `static/` — UI
- `seed_demo.py` — optional demo data
