"""Month generation, the auto-assignment algorithm, and coverage analysis.

Workers may only be present for part of a shift, so headcount varies over time.
We slice each shift into short slots (SLOT_MIN) and reason about how many people
are present in each slot. Keeping every slot within [min, max] guarantees the
time-weighted *average* headcount is also within [min, max], and — more usefully —
avoids moments that are understaffed or overcrowded.
"""
import calendar as _calendar
from datetime import date

SLOT_MIN = 15  # granularity, in minutes, for coverage reasoning


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def slot_starts(start_min, end_min):
    """Slot start-minute for each SLOT_MIN block inside [start_min, end_min)."""
    return list(range(start_min, end_min, SLOT_MIN))


def covers(av_start, av_end, slot_start):
    """True if availability [av_start, av_end) overlaps the slot at slot_start."""
    return av_start < slot_start + SLOT_MIN and av_end > slot_start


# ---------------------------------------------------------------------------
# Month / instance generation
# ---------------------------------------------------------------------------

def dates_in_month(year, month, weekday):
    """All dates in (year, month) falling on the given Python weekday (Mon=0)."""
    days = _calendar.monthrange(year, month)[1]
    out = []
    for d in range(1, days + 1):
        dt = date(year, month, d)
        if dt.weekday() == weekday:
            out.append(dt.isoformat())
    return out


def generate_instances(conn, year, month):
    """Create shift_instances for every active template across the given month.

    Idempotent: existing (template, date) rows are left untouched. Returns the
    number of new instances created.
    """
    templates = conn.execute(
        "SELECT id, weekday FROM shift_templates WHERE active = 1"
    ).fetchall()
    created = 0
    for t in templates:
        for iso in dates_in_month(year, month, t["weekday"]):
            cur = conn.execute(
                "INSERT OR IGNORE INTO shift_instances (template_id, date) VALUES (?, ?)",
                (t["id"], iso),
            )
            created += cur.rowcount
    conn.commit()
    return created


# ---------------------------------------------------------------------------
# Coverage analysis
# ---------------------------------------------------------------------------

def coverage(template, people):
    """Per-slot headcount for a shift.

    `people` is a list of dicts with 'start' and 'end' as 'HH:MM'.
    Returns a dict with the slot timeline and summary stats.
    """
    s = to_minutes(template["start_time"])
    e = to_minutes(template["end_time"])
    starts = slot_starts(s, e)
    windows = [(to_minutes(p["start"]), to_minutes(p["end"])) for p in people]

    counts = []
    for slot in starts:
        n = sum(1 for (a, b) in windows if covers(a, b, slot))
        counts.append(n)

    slots = [
        {
            "label": to_hhmm(slot),
            "count": c,
            "under": c < template["min_people"],
            "over": c > template["max_people"],
        }
        for slot, c in zip(starts, counts)
    ]
    avg = round(sum(counts) / len(counts), 2) if counts else 0
    return {
        "slots": slots,
        "avg": avg,
        "min_count": min(counts) if counts else 0,
        "max_count": max(counts) if counts else 0,
        "under_target": avg < template["min_people"],
        "over_target": avg > template["max_people"],
        "any_understaffed": any(s["under"] for s in slots),
        "any_overstaffed": any(s["over"] for s in slots),
    }


# ---------------------------------------------------------------------------
# Auto-assignment
# ---------------------------------------------------------------------------

def auto_assign(template, candidates):
    """Greedily pick people to bring every slot up to min_people.

    Candidates with if_needed=True are only used as a last resort — the
    algorithm runs a first pass using only willing candidates, then a second
    pass with if_needed candidates to fill any remaining gaps.

    `candidates` is a list of dicts: {'employee_id', 'name', 'start', 'end',
    'if_needed'} (approved availability windows).

    Returns the list of chosen candidate dicts (full availability windows).
    """
    s = to_minutes(template["start_time"])
    e = to_minutes(template["end_time"])
    starts = slot_starts(s, e)
    mn, mx = template["min_people"], template["max_people"]

    all_cand = [
        {**c, "_win": (to_minutes(c["start"]), to_minutes(c["end"]))}
        for c in candidates
    ]
    counts = {slot: 0 for slot in starts}
    chosen = []
    chosen_ids = set()

    def _greedy_pass(pool):
        """One greedy pass: keep picking from pool until no deficient slots remain."""
        while True:
            deficient = [slot for slot in starts if counts[slot] < mn]
            if not deficient:
                break
            best, best_gain = None, 0
            for c in pool:
                if c["employee_id"] in chosen_ids:
                    continue
                a, b = c["_win"]
                if any(covers(a, b, slot) and counts[slot] + 1 > mx for slot in starts):
                    continue
                gain = sum(1 for slot in deficient if covers(a, b, slot))
                if gain > best_gain:
                    best, best_gain = c, gain
            if best is None or best_gain == 0:
                break
            a, b = best["_win"]
            for slot in starts:
                if covers(a, b, slot):
                    counts[slot] += 1
            chosen.append(best)
            chosen_ids.add(best["employee_id"])

    # Pass 1: willing candidates only
    willing = [c for c in all_cand if not c.get("if_needed")]
    _greedy_pass(willing)

    # Pass 2: fill remaining gaps with if_needed candidates
    if any(counts[slot] < mn for slot in starts):
        backup = [c for c in all_cand if c.get("if_needed")]
        _greedy_pass(backup)

    return [{k: v for k, v in c.items() if k != "_win"} for c in chosen]
