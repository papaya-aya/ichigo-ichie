# Ichigo Ichie — notes for Claude Code

Shift, order, delivery and cost management for a small daifuku workshop.
Flask, server-rendered Jinja templates, no build step.

## Before you push — always

```bash
./venv/bin/python tests/smoke_test.py
```

It renders every owner page and checks the money maths. Exits non-zero on
failure. Pushing to `main` deploys to production immediately, so a broken page
is live within a minute. Two 500s reached users because a change was only
syntax-checked, never run.

## The database is PostgreSQL, not SQLite

`db.py` talks to Postgres on Vercel via psycopg2, using `DB_HOST` / `DB_PASSWORD`
env vars that exist only in Vercel. There is no `.env` locally, so `import app`
fails on its own. `tests/harness.py` installs a SQLite stand-in as
`sys.modules["db"]` before app.py is imported, which is how the smoke test runs.

`shifto.db` in the repo root is a stale June 2026 snapshot from before the
Postgres move. Do not trust it for anything current.

### Dialect traps that have bitten before

The harness is SQLite, so a passing test does **not** prove Postgres safety.
Watch for these — every one of them shipped a production outage:

- **`INSERT OR IGNORE`** is SQLite-only. Use `ON CONFLICT (cols) DO NOTHING`.
- **`LEFT(x, n)`** does not exist in SQLite. Use `SUBSTR(x, 1, n)` — works in both.
- **`INTERVAL '7 days'`** is Postgres-only. Compute the date in Python and pass
  it as a parameter.
- **A bare `%` in SQL** (e.g. `LIKE '2026-08-%'`) is read by psycopg2 as a
  format specifier and crashes. Always bind it: `LIKE ?` with `("2026-08-%",)`.
- **`date` is reserved** in Postgres. Qualify it: `orders.date`.

## Schema changes

Edit `schema.sql`. It is executed statement by statement on every startup, so
everything must be idempotent: `CREATE TABLE IF NOT EXISTS`, and
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for new columns.

- An `ALTER` must appear **after** the `CREATE TABLE` it modifies. The file runs
  top to bottom.
- **Never put a semicolon inside a comment.** `db.py` splits the file on `;`, so
  a semicolon in a comment cuts the next statement in half and breaks startup.

## One-time data migrations

Put them in `migrate_db()` in `db.py`, guarded by a flag in `settings` so they
run exactly once:

```python
if not conn.execute(
    "SELECT 1 FROM settings WHERE key='my_migration'"
).fetchone():
    ...
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('my_migration', '1')"
        " ON CONFLICT (key) DO NOTHING"
    )
    conn.commit()
```

## Templates

- **Forms cannot nest.** To put a delete button inside a row of a larger form,
  declare the small form outside the table and wire the button with
  `form="its-id"`. See `templates/summary.html`.
- `tojson` returns Markup, so `|e` after it does nothing. Put JSON in a
  **single-quoted** attribute: `data-x='{{ value | tojson }}'`.
- Pages must work on a phone. Notes and long text wrap rather than truncate.

## Money — where the numbers come from

- **Sales** are orders priced per client. Rates are *net to Ichigo Ichie per
  piece*, with revenue splits already applied — not the client's shelf price.
- **Consignment** clients (Asha Tea, Thao) bill on what they report selling,
  stored in `consignment_sales`, not on quantity delivered.
- **Pop-ups** default to everything made less 10% waste; per-event actuals in
  `popup_sales` override that.
- **Labour** comes from shift assignments, pop-up hours, strawberry purchase
  runs and delivery transport. An assigned purchase run pays whether or not it
  is ticked done.
- An ad-hoc strawberry request on the same date as an assigned run is skipped so
  it is never paid twice.

## Do not commit

Secrets, bank or card exports, or the cost spreadsheet URL. **This repo is
public** — check before adding anything with real figures or customer names in
it.
