"""Order totals, labor/productivity math, and demand-based staffing suggestions.

The link between the two halves of the business:

    orders (pieces)  ->  person-hours needed  =  pieces / target_productivity
    suggested team   =  ceil(person-hours needed / shift length in hours)
    actual productivity = pieces / person-hours actually assigned

This is computable precisely because each assignment stores a start/end window.
"""
import math

from db import FLAVORS
from scheduler import to_minutes

FLAVOR_KEYS = [f"qty_{suffix}" for suffix, _ in FLAVORS]


def order_total(order):
    """Total pieces across all flavors for one order row."""
    return sum(order[k] for k in FLAVOR_KEYS)


def orders_for_date(conn, date):
    return conn.execute(
        """SELECT o.*, c.name AS client_name
             FROM orders o JOIN clients c ON c.id = o.client_id
            WHERE o.date = ?
            ORDER BY c.name""",
        (date,),
    ).fetchall()


def day_totals(conn, date):
    """Per-flavor and grand total pieces ordered for a date."""
    rows = orders_for_date(conn, date)
    by_flavor = {suffix: 0 for suffix, _ in FLAVORS}
    for o in rows:
        for suffix, _ in FLAVORS:
            by_flavor[suffix] += o[f"qty_{suffix}"]
    return {
        "by_flavor": by_flavor,
        "total": sum(by_flavor.values()),
        "n_orders": len(rows),
    }


# ---------------------------------------------------------------------------
# Deliveries (an order is produced on `date`, shipped on `delivery_date`)
# ---------------------------------------------------------------------------

def deliveries_for_date(conn, date):
    """Orders that ship on `date` (delivery_date, falling back to production date).
    Excludes pop-up orders (is_pickup=1) since those don't need delivery."""
    return conn.execute(
        """SELECT o.*, c.name AS client_name,
                  COALESCE(o.delivery_date, o.date) AS deliver_on
             FROM orders o JOIN clients c ON c.id = o.client_id
            WHERE COALESCE(o.delivery_date, o.date) = ?
              AND (o.is_pickup IS NULL OR o.is_pickup = 0)
            ORDER BY o.delivered, c.name""",
        (date,),
    ).fetchall()


def delivery_days_in_month(conn, month):
    """One row per date in `month` that has deliveries due, with summary counts.
    Excludes pop-up orders (is_pickup=1)."""
    return conn.execute(
        """SELECT COALESCE(delivery_date, date) AS deliver_on,
                  COUNT(*) AS n,
                  SUM(delivered) AS done,
                  SUM(qty_original + qty_matcha + qty_hojicha + qty_other) AS pcs
             FROM orders
            WHERE COALESCE(delivery_date, date) LIKE ?
              AND (is_pickup IS NULL OR is_pickup = 0)
            GROUP BY deliver_on
            ORDER BY deliver_on""",
        (month + "-%",),
    ).fetchall()


def deliveries_by_date_map(conn, month):
    """date -> {n, done, pcs} for quick calendar lookups."""
    return {
        r["deliver_on"]: {"n": r["n"], "done": r["done"] or 0, "pcs": r["pcs"] or 0}
        for r in delivery_days_in_month(conn, month)
    }


def person_hours(people):
    """Sum of assigned window lengths, in hours. `people` rows have start/end 'HH:MM'."""
    total = 0
    for p in people:
        total += to_minutes(p["end"]) - to_minutes(p["start"])
    return round(total / 60.0, 2)


def shift_hours(template):
    return (to_minutes(template["end_time"]) - to_minutes(template["start_time"])) / 60.0


def staffing(template, total_pieces, assigned_people, target_productivity):
    """Demand-based staffing analysis for one shift.

    Returns suggested headcount (clamped into the min/max guardrail band),
    person-hours needed vs assigned, and the resulting actual productivity.
    """
    hrs = shift_hours(template) or 1.0
    needed_ph = (total_pieces / target_productivity) if target_productivity else 0.0
    raw_suggest = math.ceil(needed_ph / hrs) if needed_ph > 0 else 0

    mn, mx = template["min_people"], template["max_people"]
    suggested = min(max(raw_suggest, mn), mx) if total_pieces > 0 else mn
    # Flag when raw demand pushes outside the guardrails.
    below_min = total_pieces > 0 and raw_suggest < mn
    above_max = raw_suggest > mx

    assigned_ph = person_hours(assigned_people)
    actual_prod = round(total_pieces / assigned_ph, 2) if assigned_ph > 0 else None

    return {
        "target_productivity": target_productivity,
        "needed_person_hours": round(needed_ph, 2),
        "assigned_person_hours": assigned_ph,
        "raw_suggested": raw_suggest,
        "suggested": suggested,
        "below_min": below_min,
        "above_max": above_max,
        "actual_productivity": actual_prod,
        # True when we have orders but the assigned labor can't hit the target rate.
        "understaffed_for_orders": total_pieces > 0 and assigned_ph + 1e-9 < needed_ph,
    }
