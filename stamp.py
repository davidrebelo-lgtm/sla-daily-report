"""Write-back: mark in HubSpot every ticket the website counts as "Outstanding Outside SLA".

WHY (added 2026-11-18): the HubSpot reports 2(a), 2(o), 5(a) and 5(b) used to carry their own
copy of the outside-SLA rules, and drifted every time the website's rules changed (roster edits,
Clarification Required, stale-segment cleanup, Amendments Required, AA Final Review, owner checks,
per-table exclusions...). Now the website is the single source of truth: each run it stamps

    sla_report_open_outside   = "Advisor Support" | "Account Services"   (blank = not counted)
    sla_report_counted_for    = name of the person the website credits the ticket to
    sla_report_last_stamped   = when this run stamped it (UTC)
(all three are plain single-line text ticket properties)

(+ sla_report_open_step, the Action Item it was counted in, for 2(b)/2(c)/2(d))
and those reports simply filter "SLA Report - Open Outside SLA is <section>", grouped by
"SLA Report - Counted For". They match the website by construction.

The builders call record(hs, ...) wherever they add a ticket to an "Outstanding Outside SLA"
column. push(hs, tables) then applies the same name exclusions the tables use and writes only
the changes (new stamps, moved stamps, cleared stamps).

Safety: a section is only written when every one of its legs finished without error, so a
HubSpot hiccup can never wipe the stamps (the reports keep the last good numbers instead).

Runs only from stamp_job.py (GitHub Actions, every 5 min) with its own token that has
crm.objects.tickets.write. The website itself stays read-only.
"""
import datetime as _dt

PROP_SECTION = "sla_report_open_outside"
PROP_PERSON = "sla_report_counted_for"
PROP_WHEN = "sla_report_last_stamped"
PROP_STEP = "sla_report_open_step"        # Action Item the ticket was in when counted (2b/2c/2d split)
PROP_PA_OPEN = "sla_report_pa_open_by"    # set only for tickets in the "Advisor Support (Pending Action)"
                                          # table's Outstanding column (report 2(b)); name of person

# Section 2 "completed today" labels. One pair per step: result ("Within"/"Outside") + who.
# They only count on the day in PROP_TODAY_DATE, so yesterday's values drop out on their own.
PROP_TODAY_DATE = "sla_today_date"
TODAY_STEPS = {   # step key -> (result property, person property, HubSpot reports)
    "Pending Action":         ("sla_today_pa_result", "sla_today_pa_by", "PA today (Daily Stats table)"),
    "In Process":             ("sla_today_ip_result", "sla_today_ip_by", "2(k) / 2(l)"),
    "Pending Confirmation":   ("sla_today_pc_result", "sla_today_pc_by", "2(m) / 2(n)"),
    "Clarification Required": ("sla_today_cr_result", "sla_today_cr_by", "2(p) / 2(q)"),
}
TODAY_SECTION = "Advisor Support"

# section value written to HubSpot -> website table title (used for that table's name exclusions)
SECTIONS = {
    "Advisor Support": "Advisor Support Dashboard",
    "Account Services": "Account Services Dashboard",
}


def _store(hs):
    if not hasattr(hs, "_stamps"):
        hs._stamps = {"rows": {}, "failed": set(), "pa_open": {}, "today": {}}
    return hs._stamps


def record(hs, section, ticket_id, owner_id, name, step=None):
    """Called by a builder each time it counts a ticket as Outstanding Outside SLA."""
    if not ticket_id or not owner_id:
        return
    _store(hs)["rows"].setdefault(str(ticket_id), (section, str(owner_id), name, step or ""))


def record_pa_open(hs, ticket_id, name):
    """Advisor Support (Pending Action) table — Outstanding Outside SLA column (report 2(b))."""
    if ticket_id and name:
        _store(hs)["pa_open"][str(ticket_id)] = name


def record_today(hs, step, ticket_id, name, within):
    """Section 2 — a ticket that finished `step` today, Within or Outside SLA, credited to `name`."""
    if ticket_id and name:
        _store(hs)["today"].setdefault(str(ticket_id), {})[step] = ("Within" if within else "Outside", name)


def mark_failed(hs, section):
    """Called when a leg of `section` errored — that section is then left untouched this run."""
    _store(hs)["failed"].add(section)


def _desired(hs, exclude_names, exclude_by_title):
    want = {}
    st = _store(hs)
    for tid, (section, oid, name, step) in st["rows"].items():
        if section in st["failed"]:
            continue
        drop = {n.strip().casefold() for n in (exclude_names | exclude_by_title.get(SECTIONS[section], set()))}
        if str(name).strip().casefold() in drop:
            continue
        want[tid] = (section, str(name), step)
    return want


def push(hs, exclude_names, exclude_by_title):
    """Sync the stamps in HubSpot to this run's counts. Returns a short summary string."""
    st = _store(hs)
    live = [s for s in SECTIONS if s not in st["failed"]]
    if not live:
        return "stamp: skipped (every section had an error)"
    want = _desired(hs, exclude_names, exclude_by_title)

    # what HubSpot currently has stamped, for the sections we are allowed to write
    have = {}
    for r in hs.search([{"propertyName": PROP_SECTION, "operator": "HAS_PROPERTY"}],
                       [PROP_SECTION, PROP_PERSON, PROP_STEP]):
        if r.get(PROP_SECTION) not in live:          # belt-and-braces: never touch a failed section
            continue
        have[str(r["id"])] = (r.get(PROP_SECTION), str(r.get(PROP_PERSON) or ""), r.get(PROP_STEP) or "")

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    updates = []
    for tid, (section, who, step) in want.items():
        if have.get(tid) != (section, who, step):
            updates.append({"id": tid, "properties": {PROP_SECTION: section, PROP_PERSON: who,
                                                      PROP_STEP: step, PROP_WHEN: now}})
    cleared = 0
    for tid in have:
        if tid not in want:
            updates.append({"id": tid, "properties": {PROP_SECTION: "", PROP_PERSON: "", PROP_STEP: "",
                                                      PROP_WHEN: now}})
            cleared += 1

    _write(hs, updates)
    skipped = sorted(st["failed"])
    msg = (f"stamp: {len(want)} counted, {len(updates) - cleared} set/moved, {cleared} cleared"
           + (f" | NOT written (leg error): {', '.join(skipped)}" if skipped else ""))
    if TODAY_SECTION not in st["failed"]:
        msg += " | " + _push_pa_open(hs, exclude_names) + " | " + _push_today(hs, exclude_names, exclude_by_title)
    return msg


def _write(hs, updates):
    for i in range(0, len(updates), 100):
        hs._req("POST", "/crm/v3/objects/tickets/batch/update", json={"inputs": updates[i:i + 100]})


def _drop(names):
    return {n.strip().casefold() for n in names}


def _push_pa_open(hs, exclude_names):
    """Report 2(b) — sync PROP_PA_OPEN to the PA table's Outstanding column."""
    drop = _drop(exclude_names)
    want = {t: n for t, n in _store(hs)["pa_open"].items() if n.strip().casefold() not in drop}
    have = {str(r["id"]): r.get(PROP_PA_OPEN) or ""
            for r in hs.search([{"propertyName": PROP_PA_OPEN, "operator": "HAS_PROPERTY"}], [PROP_PA_OPEN])}
    ups = [{"id": t, "properties": {PROP_PA_OPEN: n}} for t, n in want.items() if have.get(t) != n]
    ups += [{"id": t, "properties": {PROP_PA_OPEN: ""}} for t in have if t not in want]
    _write(hs, ups)
    return f"2b: {len(want)} counted, {len(ups)} changed"


def _toronto_midnight_utc_ms():
    """Today's Toronto date as HubSpot stores a date property (midnight UTC, epoch ms)."""
    from hubspot_client import TZ
    d = _dt.datetime.now(TZ).date()
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def _push_today(hs, exclude_names, exclude_by_title):
    """Section 2 completed-today — sync the per-step result/person labels for today.

    Pending Action credits follow the "Advisor Support (Pending Action)" table (global exclusions
    only); the other steps follow the "Advisor Support Dashboard" table (+ its own exclusions)."""
    drop_pa = _drop(exclude_names)
    drop_dash = _drop(exclude_names | exclude_by_title.get(SECTIONS[TODAY_SECTION], set()))
    want = {}
    for tid, steps in _store(hs)["today"].items():
        keep = {st: v for st, v in steps.items()
                if v[1].strip().casefold() not in (drop_pa if st == "Pending Action" else drop_dash)}
        if keep:
            want[tid] = keep

    today = _toronto_midnight_utc_ms()
    props = [PROP_TODAY_DATE] + [p for res, who, _ in TODAY_STEPS.values() for p in (res, who)]
    have = {}
    for r in hs.search([{"propertyName": PROP_TODAY_DATE, "operator": "EQ", "value": today}], props):
        have[str(r["id"])] = {st: (r.get(res) or "", r.get(who) or "")
                              for st, (res, who, _) in TODAY_STEPS.items()}

    def full(steps):            # every step's value for one ticket ('' where it didn't finish that step today)
        return {st: steps.get(st, ("", "")) for st in TODAY_STEPS}

    ups = []
    for tid, steps in want.items():
        f = full(steps)
        if have.get(tid) != f:
            p = {PROP_TODAY_DATE: today}
            for st, (res, who, _) in TODAY_STEPS.items():
                p[res], p[who] = f[st]
            ups.append({"id": tid, "properties": p})
    for tid in have:
        if tid not in want:
            p = {PROP_TODAY_DATE: ""}
            for res, who, _ in TODAY_STEPS.values():
                p[res], p[who] = "", ""
            ups.append({"id": tid, "properties": p})
    _write(hs, ups)
    return f"today: {len(want)} tickets, {len(ups)} changed"
