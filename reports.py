"""The three Claude dashboards, each a union of CRM reports off the
"New SLA Enhancement Dashboard", aggregated per "Assigned to" person.

  open-outside      = 2(b)+2(c)+2(d)  Open Support Tickets - Outside SLA
  completed-outside = 2(e)+2(g)+2(i)  Tickets Completed Last 7 Days - Outside SLA
  completed-within  = 2(f)+2(h)+2(j)  Tickets Completed Last 7 Days - Within SLA

Each sub-report contributes a set of ticket ids -> assigned_to owner. We union by
ticket id (so a ticket counted in two sub-reports counts once) and tally per owner.
"""
from __future__ import annotations

import sys

from hubspot_client import HubSpot, P, days_ago_ms, today_bounds_ms, to_ms, to_num


def _datediff_minutes(rows, a_key, b_key):
    """The reports' 'Total Time in <stage>' formula, reproduced:
      DATEDIFF('MINUTE', <entered stage>, <exited stage>)
    i.e. whole (floored) minutes in the stage. Both are stored date properties returned
    by Search. {id: minutes|None}; None when either timestamp is missing -> unclassifiable,
    matching the report (the <=15 / >15 filter excludes null-formula rows). NOTE: the
    HubSpot properties total_time_in_process_support_ticket /
    total_time_in_pending_confirmation_support_ticket do NOT exist — this DATEDIFF is
    how the SLA time is actually obtained.

    Returns None (excluded) when the exit is missing OR precedes the entry (b < a). A
    missing or earlier exit means the ticket has NOT validly left the stage this cycle —
    it's still live, or it re-entered and the exit is stale from a prior cycle. Counting
    those would let a negative time slip under the <=15 SLA (this is what over-counted
    Hardeepika: 2n 11 -> 9 once the two re-entry tickets are dropped)."""
    out = {}
    for r in rows:
        a = to_ms(r.get(a_key))
        b = to_ms(r.get(b_key))
        out[r["id"]] = None if (a is None or b is None or b < a) else (b - a) // 60000  # floor, like DATEDIFF
    return out


def _pa_minutes(rows):
    """Pending Action time = DATEDIFF(entered In Review, entered In Process); exiting
    Pending Action == entering In Process, so this is the report 2(e)/2(f) formula."""
    return _datediff_minutes(rows, P["date_entered_in_review"], P["date_entered_in_process"])


def _resolve(hs, rows, prop):
    """Value of a classification metric per ticket: {id: float|None}.

    The SLA split relies on calculated properties (time_to_first_agent_reply,
    total_time_in_process_*, total_time_in_pending_confirmation_*). Read inline from
    the Search response these are computed lazily and occasionally come back null,
    which silently dropped or mis-bucketed tickets — the source of the intermittent
    miscounts. We take the Search value when present (fast, and what the boards were
    calibrated on) and fall back to a batch/read ONLY for the tickets Search left
    null — batch/read forces HubSpot to compute the value. This is never worse than
    reading from Search alone and recovers the ones Search dropped, so the
    classification no longer flickers with index/calc timing."""
    metric, missing = {}, []
    for r in rows:
        v = to_num(r.get(prop))
        (missing.append(r["id"]) if v is None else metric.__setitem__(r["id"], v))
    if missing:
        for tid, p in hs.batch_read(missing, [prop]).items():
            metric[tid] = to_num(p.get(prop))
    return metric


def _classify(rows, metric, threshold, within, owner_of, *, keep=None, tag=""):
    """Shared SLA split. `metric` = {id: value|None}. A ticket is WITHIN when its
    value <= threshold, OUTSIDE when > threshold. A ticket with NO value is
    **unclassifiable** — excluded from BOTH sides (never guessed into one) and
    logged, so it can neither be silently dropped nor wrongly inflate a side.
    `keep(row)` is an optional extra predicate (per-report exclusions)."""
    out, unclassified = {}, 0
    for r in rows:
        v = metric.get(r["id"])
        if v is None:
            unclassified += 1
            continue
        if (v <= threshold) != within:
            continue
        if keep and not keep(r):
            continue
        oid = owner_of(r)
        if oid:
            out[r["id"]] = oid
    if unclassified:
        print(f"[dash] {tag} ({'within' if within else 'outside'}): "
              f"{unclassified} ticket(s) had no metric value and were excluded",
              file=sys.stderr)
    return out

# Board A restricts to this 10-person support roster (report 2b/c/d filter 5).
SUPPORT_ROSTER = [
    "Hardeepika Ahluwalia", "Batuhan Karabay", "Christian Alvarez", "Ryan Connon",
    "Shivani Shaurya", "Phil Kolanowski", "Andrew Kirkham", "Ali Vahedi",
    "Adam Goldband", "Gabriel Tan",
]
OWNER_EXCLUDE = ["Stephanie Hunter", "Daniel Willett"]


# ---------------------------------------------------------------------------
# Board A — Open Support Tickets - Outside SLA (2b+2c+2d)
# ---------------------------------------------------------------------------
def build_open_outside(hs: HubSpot):
    """2(b)+2(c)+2(d): union of the 'Outside SLA - <stage> (Support Tickets)'
    segments, minus the report's exclusion filters, tallied by assigned_to. The
    segments carry the outside-SLA logic, so this tracks the report as its criteria
    evolve. No hard-coded roster — the report dropped it."""
    id_to_name, name_to_id = hs.owner_maps()
    _, closed = hs.support_ids()
    exclude_ids = {name_to_id[n.casefold()] for n in OWNER_EXCLUDE if n.casefold() in name_to_id}

    seg = hs.sla_segments()
    ids = set()
    for list_id in seg.values():
        ids.update(hs.list_members(list_id))
    props = hs.batch_read(list(ids), [P["assigned_to"], P["owner"], P["stage"],
                                       P["submitted_by"], P["in_process_reason"]])
    counts = {}
    for p in props.values():
        if p.get(P["stage"]) == closed:                       # status ≠ Closed
            continue
        owner = p.get(P["owner"])
        if owner and str(owner) in exclude_ids:               # owner ∉ {Stephanie, Daniel}, empty OK
            continue
        if "daniel willett" in (p.get(P["submitted_by"]) or "").lower():
            continue
        reason = (p.get(P["in_process_reason"]) or "").lower()
        if "nbin" in reason or "custodian" in reason:         # In Process Reason ∌ NBIN/Custodian
            continue
        a = p.get(P["assigned_to"])
        if not a:
            continue
        name = id_to_name.get(str(a), str(a))
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Board B / C — Completed Last 7 Days (Outside / Within SLA)
# ---------------------------------------------------------------------------
_PA_PROPS = [P["assigned_to_processing"], P["date_entered_in_review"], P["date_entered_in_process"]]


def _completed_pending_action(hs, within: bool):
    """2(f) within / 2(e) outside — Pending Action Completed Last 7 Days. Matches the
    report exactly: pipeline=Support, action_item != Pending Action, create date < 8
    days ago, Assigned to Processing known; SLA on the 'Total Time in Pending action'
    formula (<=15 within / >15 outside). Attributed to assigned_to_processing."""
    pid, _ = hs.support_ids()
    cutoff = days_ago_ms(8)
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["Pending Action"]},
        {"propertyName": P["assigned_to_processing"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["create_date"], "operator": "GT", "value": cutoff},   # "Create date < 8 days ago"
    ]
    rows = hs.search(filters, _PA_PROPS)
    return _classify(rows, _pa_minutes(rows), 15, within,
                     lambda r: r.get(P["assigned_to_processing"]), tag="completed PA")


def _completed_in_process(hs, within: bool):
    """2(g) outside / 2(h) within — In Process, last 7 days. Same rule as the today leg,
    windowed to the last 8 days: grouped by Date Entered In Process, action_item !=
    'In Process', SLA = DATEDIFF(entered, exited) vs 15. Attributed to
    assigned_to_support_ticket; outside also requires Assigned to Processing known."""
    pid, _ = hs.support_ids()
    cutoff = days_ago_ms(8)
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["In Process"]},
        {"propertyName": P["assigned_to_support_ticket"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["date_entered_in_process"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["date_entered_in_process"], "operator": "GT", "value": cutoff},
    ]
    if not within:  # 2(g) also requires assigned_to_processing known
        filters.append({"propertyName": P["assigned_to_processing"], "operator": "HAS_PROPERTY"})
    rows = hs.search(filters, [P["assigned_to_support_ticket"],
                               P["date_entered_in_process"], P["date_exited_in_process"]])
    mins = _datediff_minutes(rows, P["date_entered_in_process"], P["date_exited_in_process"])
    return _classify(rows, mins, 15, within,
                     lambda r: r.get(P["assigned_to_support_ticket"]), tag="completed IP")


def _completed_pending_conf(hs, within: bool):
    """2(i) outside / 2(j) within — Pending Confirmation, last 7 days. Same rule as the
    today leg, windowed to the last 8 days: grouped by Date Entered Pending Confirmation,
    action_item != 'Pending Confirmation', SLA = DATEDIFF(entered, exited) vs 15.
    Attributed to assigned_to_final_review."""
    pid, _ = hs.support_ids()
    cutoff = days_ago_ms(8)
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["Pending Confirmation"]},
        {"propertyName": P["date_entered_pending_conf"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["date_entered_pending_conf"], "operator": "GT", "value": cutoff},
    ]
    if within:  # 2(j) requires assigned_to_final_review known
        filters.append({"propertyName": P["assigned_to_final_review"], "operator": "HAS_PROPERTY"})
    rows = hs.search(filters, [P["assigned_to_final_review"], P["assigned_to_support_ticket"],
                               P["date_entered_pending_conf"], P["date_exited_pending_conf"]])
    mins = _datediff_minutes(rows, P["date_entered_pending_conf"], P["date_exited_pending_conf"])
    return _classify(rows, mins, 15, within,
                     lambda r: (r.get(P["assigned_to_final_review"])
                                or r.get(P["assigned_to_support_ticket"]) or r.get(P["assigned_to"])),
                     tag="completed PC")


def _sum_per_person(hs, subresults):
    """Per person, the SUM of their counts across the sub-reports — i.e. exactly
    what you get by adding the three CRM reports together (2f+2l+2n for Within,
    2e+2k+2m for Outside). A ticket that appears in more than one leg for the same
    person is counted once PER leg, matching the source reports (each report counts
    it independently). This mirrors the reports rather than de-duping tickets:
    the board is 'sum the three reports', per the report owners' definition."""
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for sr in subresults:                        # sr is {ticket_id: owner_id}
        for oid in sr.values():
            if not oid:
                continue
            name = id_to_name.get(str(oid), str(oid))
            counts[name] = counts.get(name, 0) + 1
    return counts


def build_completed(hs: HubSpot, within: bool):
    return _sum_per_person(hs, [
        _completed_pending_action(hs, within),
        _completed_in_process(hs, within),
        _completed_pending_conf(hs, within),
    ])


# Per-stage breakdown of the Completed-Last-7-Days board — each stage kept SEPARATE
# (not summed), for the stacked ?report=completed-stages view.
_COMPLETED_STAGE_LEGS = [
    ("Pending Action", _completed_pending_action),
    ("In Process", _completed_in_process),
    ("Pending Confirmation", _completed_pending_conf),
]


def build_completed_stage(hs: HubSpot, within: bool):
    """[(stage_label, {name: count}), ...] in stage order — the three Completed Last
    7 Days legs tallied per person individually (2e/2g/2i outside, 2f/2h/2j within)."""
    return [(label, _sum_per_person(hs, [fn(hs, within)]))
            for label, fn in _COMPLETED_STAGE_LEGS]


# ---------------------------------------------------------------------------
# Completed TODAY (2k/2l In Process, 2m/2n Pending Confirmation, 2e/2f Pending
# Action's today row). Keyed off the day each ticket COMPLETED the stage — i.e.
# "Date Exited <stage> is Today" (PA completes by entering In Process) — matching
# HubSpot's 'Completed Today' report, which counts by completion day, not entry day.
# No 'create date < 8 days' filter on the today columns. In-Process SLA splits at 15.
# Reconciled 2026-07-23 against HubSpot for Hardeepika (Jul-22 Outside = 20:
# 6 In Process + 14 Pending Confirmation); entry-day grouping had undercounted her to 12.
# ---------------------------------------------------------------------------
def _today_pending_action(hs, within: bool):
    """2(e)/2(f) 'today row' — COMPLETED today. The board groups the today columns by
    the day a ticket *left* (completed) the stage, matching HubSpot's 'Completed Today'
    report — NOT the day it entered. Exiting Pending Action == entering In Process, so
    the completion timestamp is date_entered_in_process; today's row = tickets that
    moved into In Process today. No 'create date < 8 days' filter (HubSpot's today
    columns count older tickets that complete today too). SLA on the Pending-Action
    formula (<=15 within / >15 outside). Attributed to assigned_to_processing."""
    pid, _ = hs.support_ids()
    t0, t1 = today_bounds_ms()
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["Pending Action"]},
        {"propertyName": P["assigned_to_processing"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["date_entered_in_process"], "operator": "GTE", "value": t0},
        {"propertyName": P["date_entered_in_process"], "operator": "LT", "value": t1},
    ]
    rows = hs.search(filters, _PA_PROPS)
    return _classify(rows, _pa_minutes(rows), 15, within,
                     lambda r: r.get(P["assigned_to_processing"]), tag="today PA")


def _today_in_process(hs, within: bool):
    """2(k)/2(l) — In Process, COMPLETED today. Grouped by **Date Exited In Process**
    (the day the ticket left the stage = the day it was completed), matching HubSpot's
    'Completed Today'. action_item != 'In Process', SLA = DATEDIFF(entered, exited In
    Process) vs 15. No 'create date < 8 days' filter — a ticket opened weeks ago that
    finishes In Process today still counts today. Attributed to assigned_to_support_ticket.
    Outside (2k) additionally requires Assigned to Processing known (and reason not
    NBIN/Custodian, owner not Daniel)."""
    pid, _ = hs.support_ids()
    t0, t1 = today_bounds_ms()
    _, name_to_id = hs.owner_maps()
    daniel = name_to_id.get("daniel willett")
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["In Process"]},
        {"propertyName": P["assigned_to_support_ticket"], "operator": "HAS_PROPERTY"},
        {"propertyName": P["date_exited_in_process"], "operator": "GTE", "value": t0},
        {"propertyName": P["date_exited_in_process"], "operator": "LT", "value": t1},
    ]
    if not within:  # 2(k) also requires assigned_to_processing known
        filters.append({"propertyName": P["assigned_to_processing"], "operator": "HAS_PROPERTY"})
    rows = hs.search(filters, [P["assigned_to_support_ticket"], P["owner"],
                               P["date_entered_in_process"], P["date_exited_in_process"]])
    mins = _datediff_minutes(rows, P["date_entered_in_process"], P["date_exited_in_process"])

    # NBIN/Custodian reason exclusion is NOT part of the Completed report (only the
    # Open-Outside board 2c uses it): a ticket that finished In Process today counts
    # even if it had been waiting on NBIN. Reconciled against HubSpot 2026-07-23 —
    # keeping the two NBIN-reason tickets is what makes Hardeepika = 20 (not 18).
    def keep(r):
        return not (daniel and str(r.get(P["owner"])) == daniel)   # owner ≠ Daniel Willett

    return _classify(rows, mins, 15, within,
                     lambda r: r.get(P["assigned_to_support_ticket"]), keep=keep, tag="today IP")


def _today_pending_conf(hs, within: bool):
    """2(m)/2(n) — Pending Confirmation, COMPLETED today. Grouped by **Date Exited Pending
    Confirmation** (the day the ticket left the stage = the day it was completed), matching
    HubSpot's 'Completed Today'. The ticket must no longer be in Pending Confirmation
    (action_item != 'Pending Confirmation'), SLA = DATEDIFF(entered, exited Pending
    Confirmation) vs 15. Attributed to assigned_to_final_review."""
    pid, _ = hs.support_ids()
    t0, t1 = today_bounds_ms()
    filters = [
        {"propertyName": P["pipeline"], "operator": "EQ", "value": pid},
        {"propertyName": P["action_item"], "operator": "NOT_IN", "values": ["Pending Confirmation"]},
        {"propertyName": P["date_exited_pending_conf"], "operator": "GTE", "value": t0},
        {"propertyName": P["date_exited_pending_conf"], "operator": "LT", "value": t1},
    ]
    if within:  # 2(n) requires assigned_to_final_review known
        filters.append({"propertyName": P["assigned_to_final_review"], "operator": "HAS_PROPERTY"})
    rows = hs.search(filters, [P["assigned_to_final_review"], P["assigned_to_support_ticket"],
                               P["date_entered_pending_conf"], P["date_exited_pending_conf"]])
    mins = _datediff_minutes(rows, P["date_entered_pending_conf"], P["date_exited_pending_conf"])
    return _classify(rows, mins, 15, within,
                     lambda r: (r.get(P["assigned_to_final_review"])
                                or r.get(P["assigned_to_support_ticket"]) or r.get(P["assigned_to"])),
                     tag="today PC")


def build_today(hs: HubSpot, within: bool):
    return _sum_per_person(hs, [
        _today_pending_action(hs, within),
        _today_in_process(hs, within),
        _today_pending_conf(hs, within),
    ])


# ---------------------------------------------------------------------------
# Reconciliation diagnostic (never shown on the TV; ?debug=1 only).
# Exposes each person's per-leg composition so board numbers can be checked
# against the source reports leg-by-leg, and so the summed total vs the
# distinct-ticket count are visible side by side.
# ---------------------------------------------------------------------------
_TODAY_LEGS = {
    "within": [("PA · 2(f) today-row", _today_pending_action),
               ("IP · 2(l)", _today_in_process),
               ("PC · 2(n)", _today_pending_conf)],
    "outside": [("PA · 2(e) today-row", _today_pending_action),
                ("IP · 2(k)", _today_in_process),
                ("PC · 2(m)", _today_pending_conf)],
}


def today_breakdown(hs: HubSpot, within: bool):
    """{person: {"legs": {leg_name: [ticket_ids]}, "summed": int, "distinct": int}}.

    `summed` = what the board currently shows (a ticket counts once per leg it
    lands in). `distinct` = unique ticket ids credited to that person across all
    legs. When summed > distinct for a person, that person handled >1 stage of the
    same ticket today and is being multi-counted."""
    id_to_name, _ = hs.owner_maps()
    legs = _TODAY_LEGS["within" if within else "outside"]
    people = {}
    for leg_name, fn in legs:
        for tid, oid in fn(hs, within).items():
            if not oid:
                continue
            name = id_to_name.get(str(oid), str(oid))
            rec = people.setdefault(name, {})
            rec.setdefault(leg_name, []).append(tid)
    out = {}
    for name, rec in people.items():
        all_ids = [t for ids in rec.values() for t in ids]
        out[name] = {"legs": rec, "summed": len(all_ids),
                     "distinct": len(set(all_ids))}
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
REPORTS = {
    "open-outside": {
        "title": "Open Support Tickets Outside SLA",
        "label": "Advisor Support · Service Delivery",
        "build": lambda hs: build_open_outside(hs),
    },
    "completed-outside": {
        "title": "Tickets Completed Last 7 Days — Outside SLA",
        "label": "Advisor Support · Service Delivery",
        "build": lambda hs: build_completed(hs, within=False),
    },
    "completed-within": {
        "title": "Tickets Completed Last 7 Days — Within SLA",
        "label": "Advisor Support · Service Delivery",
        "build": lambda hs: build_completed(hs, within=True),
    },
    "today-outside": {
        "title": "Tickets Completed Today — Outside SLA",
        "label": "Advisor Support · Service Delivery",
        "build": lambda hs: build_today(hs, within=False),
    },
    "today-within": {
        "title": "Tickets Completed Today — Within SLA",
        "label": "Advisor Support · Service Delivery",
        "build": lambda hs: build_today(hs, within=True),
    },
}
DEFAULT_REPORT = "open-outside"
