"""Daily SLA Report — table builders, pulled LIVE from HubSpot.

Standalone (no Streamlit). Reuses the exact, validated logic from the dashboard app:
hubspot_client.py (the API client) and reports.py (the Advisor Support boards).

build_tables(hs) -> ordered list of table dicts matching the daily SLA email:
  {"title","columns","rows","total"}      for people tables
  {"title","columns","flat_row"}          for the aggregate NBIN tables
"""
import datetime as _dt

import reports
from hubspot_client import to_ms, to_num, today_bounds_ms, TZ

# Test/demo owners to exclude from every people table (they aren't real reps).
_EXCLUDE_NAMES = {"Transition Demo"}


# ── Client Service Dashboard: 4b "Tickets Outside SLA" (full dataset tree) ──────────────
_CS_PIPELINES = ["82286254", "82318988", "145543234", "82088341", "82231167"]  # Transfer, Add Funds, New Accounts, Withdraw, Plans
_ACS_ACTION_ITEMS = {"Enhanced Review", "Pending Action", "Pending Confirmation", "Transmitted",
                     "Pending Final Review", "Account Opening", "Opening Account",
                     "Amendments Required", "Pending Signature", "Pending paperwork"}
_CLIENT_SIG_ROLES = ["Account Holder", "Annuitant", "Authorized Third Party", "Beneficiary", "Client",
                     "Executor", "Individual of Authority", "Joint Relinquishing Plan Holder",
                     "Joint Subscriber", "Legal Guardian", "Primary Caregiver", "Principal Beneficiary",
                     "Receiving Account Holder", "Relinquishing Issuer", "Relinquishing Plan Holder",
                     "Spouse", "Subscriber", "Trustee", "Witness"]
_ADVISOR_SIG_ROLES = ["ID Verifier"]
_PM_SIG_ROLES = ["Portfolio Manager", "Supervisor", "Authorized Supervisor", "Investment Advisor"]
_4B_FIELDS = ["action_item", "hs_pipeline", "hs_pipeline_stage", "note_status", "follow_up_with",
              "follow_up_date", "note_follow_up_date", "envelope_needs_to_sign", "sla_due_date",
              "assigned_to", "hubspot_owner_id", "owner_config", "portfolio_manager",
              "associate_portfolio_manager", "supervising_portfolio_manager", "request_type",
              "action_item_sla", "assigned_to_outside_sla", "sent_to_nbin__date__time",
              "date_entered_in_process_support_ticket"]


def _days_ago_edt_midnight(days):
    """HubSpot 'is less than N days ago (EDT)' rounds to the EDT day boundary, NOT a
    rolling now-N*24h window (verified 2026-08-18: report 6d = 55 with this)."""
    start = _dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    d = start - _dt.timedelta(days=days)
    return int(d.astimezone(_dt.timezone.utc).timestamp() * 1000)


def _cs_label_maps(hs):
    data = hs._req("GET", "/crm/v3/pipelines/tickets").get("results", [])
    pl, st = {}, {}
    for p in data:
        pl[str(p.get("id"))] = p.get("label")
        for s in p.get("stages", []):
            st[str(s.get("id"))] = s.get("label")
    return pl, st


def _4b_outside(hs):
    """Report 4b — Client Service tickets currently Outside SLA (12-clause dataset tree)."""
    now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    pl_label, st_label = _cs_label_maps(hs)
    id_to_name, _ = hs.owner_maps()

    def has(text, subs):
        t = text or ""
        return any(s in t for s in subs)

    def keep(p):
        ai = p.get("action_item"); req = p.get("request_type")
        if req in ("Cancel / Correct", "Residual Transfer-In Sweep"):
            return True
        due = to_ms(p.get("sla_due_date"))
        if due is None:
            return False
        if not (int((due - now_ms) / 3600000) < 0):
            return False
        if p.get("hs_pipeline") not in _CS_PIPELINES:
            return False
        if (st_label.get(str(p.get("hs_pipeline_stage")), "") or "").strip() == "Trade Processing":
            return False
        if ai in ("Account Opening", "Opening Account"):
            return False
        ns = p.get("note_status")
        if not (ns in (None, "") or ns == "Closed"):
            return False
        stn = to_ms(p.get("sent_to_nbin__date__time"))
        if stn is None:
            c11 = True
        else:
            ep = to_ms(p.get("date_entered_in_process_support_ticket"))
            c11 = ep is not None and int((stn - ep) / 60000) > 15
        if not c11:
            return False
        if ai in ("Pending Confirmation", "Pending Final Review") and req == "Initiate a transfer":
            return False
        if req == "Manage plan" and ai == "Pending Confirmation":
            return False
        ais = to_num(p.get("action_item_sla"))
        sla_out = (ai != "Pending Final Review") and (ais != 0) and (now_ms > due)
        env = p.get("envelope_needs_to_sign")
        if not ((ai in _ACS_ACTION_ITEMS) or has(env, ["Optimize Onboarding"])):
            return False
        assigned, owner, oc = p.get("assigned_to"), p.get("hubspot_owner_id"), p.get("owner_config")
        pending_client = has(env, _CLIENT_SIG_ROLES)
        pend_adv = ((assigned and assigned == owner and oc in ("Config 2", "Config 3", "Config 4", "Config 5"))
                    or has(env, _ADVISOR_SIG_ROLES) or (ns == "Open" and p.get("follow_up_with") == "Advisor"))
        pend_pm = ((assigned and assigned in (p.get("associate_portfolio_manager"), p.get("portfolio_manager"),
                                              p.get("supervising_portfolio_manager")))
                   or has(env, _PM_SIG_ROLES) or (ns == "Open" and p.get("follow_up_with") == "Portfolio Manager"))
        pending_custodian = (ai in ("Account Opening", "Opening Account")) or (ns == "Open" and p.get("follow_up_with") == "Custodian")
        if pending_client or pend_adv or pend_pm or pending_custodian:
            return False
        stagelbl = st_label.get(str(p.get("hs_pipeline_stage")), "") or ""
        pipelbl = pl_label.get(str(p.get("hs_pipeline")), "") or ""
        fud = to_ms(p.get("follow_up_date")); nfud = to_ms(p.get("note_follow_up_date"))
        transfer_fu = (stagelbl == "Transfer Initiated" and ai == "Pending Confirmation"
                       and ((fud is not None and fud <= now_ms) or (fud is None and sla_out)))
        gen_fu = (stagelbl != "Transfer Initiated" and (fud is not None and fud <= now_ms) and sla_out)
        open_note_fu = (pipelbl in ("Add Funds", "New Accounts") and (nfud is not None and nfud <= now_ms) and ns == "Open")
        future_fu = ((stagelbl == "Transfer Initiated" and ai == "Pending Confirmation" and (fud is not None and fud > now_ms) and sla_out)
                     or (stagelbl != "Transfer Initiated" and (fud is not None and fud > now_ms) and sla_out)
                     or (pipelbl in ("Add Funds", "New Accounts") and ai == "Pending Final Review" and ns == "Open"
                         and (nfud is not None and nfud > now_ms) and sla_out))
        if transfer_fu or gen_fu or open_note_fu or future_fu:
            return False
        return True

    counts, seen = {}, set()

    def process(rows):
        for p in rows:
            tid = p.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            if keep(p):
                attr = p.get("assigned_to")
                if attr:
                    nm = id_to_name.get(str(attr), str(attr))
                    counts[nm] = counts.get(nm, 0) + 1

    process(hs.search([{"propertyName": "hs_pipeline", "operator": "IN", "values": _CS_PIPELINES},
                       {"propertyName": "sla_due_date", "operator": "LT", "value": now_ms},
                       {"propertyName": "sla_due_date", "operator": "HAS_PROPERTY"}], _4B_FIELDS))
    process(hs.search([{"propertyName": "request_type", "operator": "IN",
                        "values": ["Cancel / Correct", "Residual Transfer-In Sweep"]},
                       {"propertyName": "hs_pipeline", "operator": "IN", "values": _CS_PIPELINES}],
                      _4B_FIELDS))
    return counts


_CS_COMPLETED_STAGES = ["187223990", "154816395", "154811555", "154790244", "154782076"]


def _cs_completed(hs):
    """Reports 4f (Completed Within SLA) / 4h (Completed Outside SLA), by Assigned to Processing.
    Verbatim from the report panels (verified live 2026-08-18)."""
    t0, t1 = today_bounds_ms()
    id_to_name, _ = hs.owner_maps()
    rows = hs.search([{"propertyName": "hs_pipeline_stage", "operator": "IN", "values": _CS_COMPLETED_STAGES},
                      {"propertyName": "closed_date", "operator": "GTE", "value": t0},
                      {"propertyName": "closed_date", "operator": "LT", "value": t1},
                      {"propertyName": "assigned_to_processing", "operator": "HAS_PROPERTY"}],
                     ["overall_sla_within", "sla_status", "total_time_with_nbin", "assigned_to_processing"])
    within, outside = {}, {}
    for p in rows:
        ov = (p.get("overall_sla_within") or "")
        sla = p.get("sla_status")
        sla_outside = (sla == "outside sla")
        f_keep = not (ov == "false" and sla_outside)
        nbin = to_num(p.get("total_time_with_nbin"))
        c5 = (nbin is None) or (nbin <= 2)
        h_keep = ((ov in ("false", "")) or sla_outside) and c5
        attr = p.get("assigned_to_processing")
        if not attr:
            continue
        nm = id_to_name.get(str(attr), str(attr))
        if f_keep:
            within[nm] = within.get(nm, 0) + 1
        if h_keep:
            outside[nm] = outside.get(nm, 0) + 1
    return within, outside


def _client_service_dashboard(hs):
    open_outside = _4b_outside(hs)
    within, outside = _cs_completed(hs)
    names = sorted(set(within) | set(outside) | set(open_outside))
    rows = [[n, within.get(n, 0), outside.get(n, 0), open_outside.get(n, 0)] for n in names]
    total = ["Total", sum(within.values()), sum(outside.values()), sum(open_outside.values())]
    return {"title": "Client Service Dashboard",
            "columns": ["Name", "Completed Within SLA", "Completed Outside SLA", "Tickets Outside SLA"],
            "rows": rows, "total": total}


# ── Transfers (Pending Review) — 3a / 3d, by Assigned to Processing ─────────────────────
def _transfers(hs):
    today = _dt.datetime.now(TZ).date()
    start = _dt.datetime(today.year, today.month, today.day, tzinfo=_dt.timezone.utc)
    t0 = int(start.timestamp() * 1000)
    t1 = int((start + _dt.timedelta(days=1)).timestamp() * 1000)
    id_to_name, _ = hs.owner_maps()

    def _tally(filters):
        out = {}
        for r in hs.search(filters, ["assigned_to_processing"]):
            oid = r.get("assigned_to_processing")
            if oid:
                nm = id_to_name.get(str(oid), str(oid))
                out[nm] = out.get(nm, 0) + 1
        return out

    due = _tally([  # 3a
        {"propertyName": "request_type", "operator": "EQ", "value": "Initiate a transfer"},
        {"propertyName": "follow_up_date_fixed_date", "operator": "GTE", "value": t0},
        {"propertyName": "follow_up_date_fixed_date", "operator": "LT", "value": t1},
        {"propertyName": "action_item", "operator": "NEQ", "value": "Cancelled"},
    ])
    outside = _tally([  # 3d
        {"propertyName": "request_type", "operator": "EQ", "value": "Initiate a transfer"},
        {"propertyName": "action_item", "operator": "EQ", "value": "Pending Final Review"},
        {"propertyName": "follow_up_date", "operator": "LT", "value": t0},
        {"propertyName": "follow_up_date", "operator": "HAS_PROPERTY"},
    ])
    names = sorted(set(due) | set(outside))
    rows = [[n, outside.get(n, 0), due.get(n, 0)] for n in names]
    total = ["Total", sum(outside.values()), sum(due.values())]
    return {"title": "Transfers (Pending Review)",
            "columns": ["Name", "Tickets Outside SLA", "Pending Review — Due Today"],
            "rows": rows, "total": total}


# ── Account Services Dashboard — 5c / 5e / 5f ──────────────────────────────────────────
_AA_STAGES = [
    ("date_entered_enhanced_review", "enhanced_review_sla_account_administration"),
    ("date_entered_transmitted", "transmitted_sla_account_administration"),
    ("date_entered_preparing_paperwork", "preparing_paperwork_sla_account_administration"),
    ("date_entered_in_review_pending_action", "pending_action_sla_account_administration"),
]
_AA_READ = ["hubspot_owner_id", "portfolio_manager", "supervising_portfolio_manager",
            "assigned_to_outside_sla", "assigned_to_within_sla", "assigned_to",
            "total_time_with_nbin", "request_type",
            "date_entered_in_process_support_ticket", "sent_to_nbin__date__time"]


def _find_list_id(hs, keywords):
    data = hs._req("POST", "/crm/v3/lists/search", json={"query": keywords[0], "count": 100})
    for l in data.get("lists", []):
        name = (l.get("name") or "").lower()
        if l.get("objectTypeId") == "0-5" and all(k in name for k in keywords):
            return l.get("listId")
    return None


_ACCT_ADMIN_REQ_TYPES = [
    "Update Account Documentation", "Modify banking", "Amend previous year tax returns",
    "RESP Breakdown", "Tax Slip Corrections", "Update Phone Number", "Add banking",
    "Delete banking", "Designation and change of beneficiary", "Client Consent Form",
    "Add/Update POA", "Close Account", "Clerical Error Update", "Tax Slip Duplicates",
    "Update SIN", "Update Entity", "Signature for Locked-In Agreements", "Update Address",
    "Update Marital Status", "Update Email", "Estate Processing", "Book Value Adjustment",
    "Change delivery method", "Update Name", "Third Party Contribution Authorization",
    "RESP beneficiary information update", "Update DOB", "Recalculate LIF Maximum",
    "Third party online access", "Update Account Legislation", "Password reset",
]


def _aa_report(hs, *, sla_value, date_mode, owner_checks, exclude_admin, total_time_clause,
               segment_keywords, nbin_branch, attribution_field):
    ids = set()
    for date_prop, sla_prop in _AA_STAGES:
        f = [{"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
             {"propertyName": sla_prop, "operator": "EQ", "value": sla_value}]
        if date_mode == "today":
            t0, t1 = today_bounds_ms()
            f += [{"propertyName": date_prop, "operator": "GTE", "value": t0},
                  {"propertyName": date_prop, "operator": "LT", "value": t1}]
        else:  # "8days" -> EDT-midnight boundary
            f += [{"propertyName": date_prop, "operator": "GTE", "value": _days_ago_edt_midnight(8)}]
        for r in hs.search(f, [date_prop]):
            ids.add(str(r["id"]))
    if segment_keywords:
        lid = _find_list_id(hs, segment_keywords)
        if lid:
            ids.update(str(x) for x in hs.list_members(lid))
    if nbin_branch:
        f = [{"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
             {"propertyName": "pending_confirmation_sla_account_administration", "operator": "EQ", "value": "Outside SLA"},
             {"propertyName": "sent_to_nbin__date__time", "operator": "HAS_PROPERTY"}]
        for r in hs.search(f, ["date_entered_in_process_support_ticket", "sent_to_nbin__date__time"]):
            a = to_ms(r.get("date_entered_in_process_support_ticket"))
            b = to_ms(r.get("sent_to_nbin__date__time"))
            if a is not None and b is not None and int((b - a) / 60000) > 15:
                ids.add(str(r["id"]))
    if not ids:
        return {}
    props = hs.batch_read(list(ids), _AA_READ)
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for p in props.values():
        if p.get("request_type") not in _ACCT_ADMIN_REQ_TYPES:
            continue
        if exclude_admin and str(p.get("assigned_to")) == "104417029":
            continue
        if total_time_clause:
            t = to_num(p.get("total_time_with_nbin"))
            if t is not None and t > 2:
                continue
        aos = p.get("assigned_to_outside_sla")
        if aos and any(str(p.get(of)) == str(aos) for of in owner_checks):
            continue
        attr = p.get(attribution_field)
        if not attr:
            continue
        nm = id_to_name.get(str(attr), str(attr))
        counts[nm] = counts.get(nm, 0) + 1
    return counts


# ── Account Services "Tickets Outside SLA" — report 5b (segment-driven snapshot) ────────
_AA_COMPLETED_STAGE = "154789384"  # "Completed (Account Administration)" stage

# 5b outside-SLA segments (HubSpot active lists), matched case-insensitively by keyword.
_5B_SEGMENTS = {
    "enhanced_review":      ["outside sla", "enhanced review", "account administration"],
    "pending_action":       ["outside sla", "pending action", "account administration"],
    "transmitted":          ["outside sla", "transmitted", "account administration"],
    "pending_confirmation": ["outside sla", "pending confirmation", "account administration"],
}


def _acct_services_outside_5b(hs):
    """Report 5b — Account Administration tickets currently Outside SLA.

    Report filter: (1 AND 2) OR (1 AND 3 AND 4)
      1 = request_type IN AA types  AND  stage != 'Completed (Account Administration)'
      2 = member of Outside-SLA segment {Enhanced Review | Pending Action | Transmitted}
      3 = NBIN Follow Up SLA > 15, where
          NBIN Follow Up SLA = DATEDIFF(MINUTE, notification_sent_to_assignee, sent_to_nbin__date__time)
      4 = member of Outside-SLA segment {Pending Confirmation}
    Live snapshot (no date window). Grouped per person by 'Assigned to' (assigned_to)."""
    seg = {}
    for key, kw in _5B_SEGMENTS.items():
        lid = _find_list_id(hs, kw)
        seg[key] = set(str(x) for x in hs.list_members(lid)) if lid else set()
    branch2 = seg["enhanced_review"] | seg["pending_action"] | seg["transmitted"]
    branch4 = seg["pending_confirmation"]
    cand = branch2 | branch4
    if not cand:
        return {}
    props = hs.batch_read(list(cand),
                          ["request_type", "hs_pipeline_stage", "assigned_to",
                           "notification_sent_to_assignee", "sent_to_nbin__date__time"])
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for tid, p in props.items():
        tid = str(tid)
        # clause 1
        if p.get("request_type") not in _ACCT_ADMIN_REQ_TYPES:
            continue
        if str(p.get("hs_pipeline_stage")) == _AA_COMPLETED_STAGE:
            continue
        # clause 3: NBIN Follow Up SLA (minutes) > 15
        a = to_ms(p.get("notification_sent_to_assignee"))
        b = to_ms(p.get("sent_to_nbin__date__time"))
        c3 = (a is not None and b is not None and int((b - a) / 60000) > 15)
        # (1 AND 2) OR (1 AND 3 AND 4)  — clause 1 already enforced above
        if (tid in branch2) or (c3 and tid in branch4):
            attr = p.get("assigned_to")
            if not attr:
                continue
            nm = id_to_name.get(str(attr), str(attr))
            counts[nm] = counts.get(nm, 0) + 1
    return counts


def _account_services(hs):
    OWNER = "hubspot_owner_id"; PM = "portfolio_manager"; SPM = "supervising_portfolio_manager"
    within = _aa_report(hs, sla_value="Within SLA", date_mode="today", owner_checks=[OWNER],
                        exclude_admin=False, total_time_clause=False,
                        segment_keywords=["account opening completed today", "within"],
                        nbin_branch=False, attribution_field="assigned_to_within_sla")            # 5f
    outside = _aa_report(hs, sla_value="Outside SLA", date_mode="today", owner_checks=[OWNER, PM, SPM],
                         exclude_admin=True, total_time_clause=True,
                         segment_keywords=["account opening completed today", "outside"],
                         nbin_branch=False, attribution_field="assigned_to_outside_sla")           # 5e
    open_outside = _acct_services_outside_5b(hs)                                                    # 5b
    names = sorted(set(within) | set(outside) | set(open_outside))
    rows = [[n, within.get(n, 0), outside.get(n, 0), open_outside.get(n, 0)] for n in names]
    total = ["Total", sum(within.values()), sum(outside.values()), sum(open_outside.values())]
    return {"title": "Account Services Dashboard",
            "columns": ["Name", "Completed Within SLA", "Completed Outside SLA", "Tickets Outside SLA"],
            "rows": rows, "total": total}


# ── Account Administration Tickets With NBIN — 6d / 6g ─────────────────────────────────
def _account_admin_nbin(hs):
    filters = [
        {"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
        {"propertyName": "assigned_to", "operator": "NOT_IN", "values": ["104417029"]},
        {"propertyName": "action_item", "operator": "IN", "values": ["Closed", "Completed"]},
        {"propertyName": "closed_date", "operator": "GTE", "value": _days_ago_edt_midnight(8)},
        {"propertyName": "notification_sent_to_assignee", "operator": "HAS_PROPERTY"},
    ]
    with_nbin = len(hs.search(filters, ["request_type"]))
    # 6g = segment "Outside SLA - Pending Confirmation (Account Administration)", reproduced live
    # (verified 2026-08-18 = 30): AA pipeline + Pending Confirmation + Time in Current Action Item
    # > 2 days (rolling) intersected with the account-admin request types and not-Completed stage.
    now2 = int((_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)).timestamp() * 1000)
    outside = len(hs.search([
        {"propertyName": "hs_pipeline", "operator": "EQ", "value": "82170383"},
        {"propertyName": "action_item", "operator": "EQ", "value": "Pending Confirmation"},
        {"propertyName": "date_entered_current_action_item", "operator": "LT", "value": now2},
        {"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
        {"propertyName": "hs_pipeline_stage", "operator": "NEQ", "value": "154789384"},
    ], ["request_type"]))
    return {"title": "Account Administration Tickets with NBIN",
            "columns": ["Tickets With NBIN", "Completed Outside SLA"],
            "flat_row": [with_nbin, outside]}


# ── Advisor Support Tickets With NBIN — 6a / 6b ────────────────────────────────────────
def _advisor_support_nbin(hs):
    CLOSED = "208647293"
    filters = [
        {"propertyName": "hs_pipeline", "operator": "EQ", "value": "117451896"},
        {"propertyName": "sent_to_nbin__date__time", "operator": "HAS_PROPERTY"},
        {"propertyName": "received_response_from_nbin", "operator": "NOT_HAS_PROPERTY"},
        {"propertyName": "hs_pipeline_stage", "operator": "NEQ", "value": CLOSED},
    ]
    rows = hs.search(filters, ["date_entered_in_process_support_ticket", "sent_to_nbin__date__time"])
    within = outside = 0
    for r in rows:
        a = to_ms(r.get("date_entered_in_process_support_ticket"))
        b = to_ms(r.get("sent_to_nbin__date__time"))
        if a is None or b is None:
            continue
        mins = int((b - a) / 60000)
        if mins <= 15:
            within += 1
        else:
            outside += 1
    return {"title": "Advisor Support Tickets With NBIN",
            "columns": ["Actioned Within SLA", "Actioned Outside SLA",
                        "Total Advisor Support Tickets with NBIN"],
            "flat_row": [within, outside, within + outside]}


# ── Advisor Support (Pending Action) + Advisor Support Dashboard (via reports.py) ──────
_ADVISOR_COLS = ["Name", "Tickets Completed Within SLA", "Tickets Completed Outside SLA", "Tickets Outside SLA"]


def _people_tbl(title, w, o, oo, cols):
    names = sorted(set(w) | set(o) | set(oo))
    rows = [[n, w.get(n, 0), o.get(n, 0), oo.get(n, 0)] for n in names]
    return {"title": title, "columns": cols, "rows": rows,
            "total": ["Total", sum(w.values()), sum(o.values()), sum(oo.values())]}


def _advisor_pending_action(hs):
    w_pa = reports._sum_per_person(hs, [reports._today_pending_action(hs, True)])
    o_pa = reports._sum_per_person(hs, [reports._today_pending_action(hs, False)])
    open_pa = {}
    seg = hs.sla_segments().get("Pending Action")
    if seg:
        id_to_name, _ = hs.owner_maps()
        for p in hs.batch_read(hs.list_members(seg), [reports.P["assigned_to_processing"]]).values():
            oid = p.get(reports.P["assigned_to_processing"])
            if oid:
                nm = id_to_name.get(str(oid), str(oid))
                open_pa[nm] = open_pa.get(nm, 0) + 1
    return _people_tbl("Advisor Support (Pending Action) — Daily Stats", w_pa, o_pa, open_pa, _ADVISOR_COLS)


def _advisor_dashboard(hs):
    return _people_tbl("Advisor Support Dashboard",
                       reports.build_today(hs, True), reports.build_today(hs, False),
                       reports.build_open_outside(hs), _ADVISOR_COLS)


def build_tables(hs):
    """Every table in the daily SLA email, in order, pulled live. Each builder is wrapped so
    one failing table degrades to a note instead of killing the whole report."""
    specs = [
        ("Advisor Support (Pending Action) — Daily Stats", _advisor_pending_action, _ADVISOR_COLS),
        ("Advisor Support Dashboard", _advisor_dashboard, _ADVISOR_COLS),
        ("Advisor Support Tickets With NBIN", _advisor_support_nbin,
         ["Actioned Within SLA", "Actioned Outside SLA", "Total Advisor Support Tickets with NBIN"]),
        ("Account Administration Tickets with NBIN", _account_admin_nbin,
         ["Tickets With NBIN", "Completed Outside SLA"]),
        ("Account Services Dashboard", _account_services,
         ["Name", "Completed Within SLA", "Completed Outside SLA", "Tickets Outside SLA"]),
        ("Transfers (Pending Review)", _transfers,
         ["Name", "Tickets Outside SLA", "Pending Review — Due Today"]),
        ("Client Service Dashboard", _client_service_dashboard,
         ["Name", "Completed Within SLA", "Completed Outside SLA", "Tickets Outside SLA"]),
    ]
    tables = []
    for title, fn, cols in specs:
        try:
            tables.append(fn(hs))
        except Exception as e:
            tables.append({"title": title, "columns": cols, "rows": [],
                           "total": (["Total"] + [0] * (len(cols) - 1)) if "Name" in cols else None,
                           "flat_row": ([0] * len(cols)) if "Name" not in cols else None,
                           "note": f"(temporarily unavailable: {e})"})

    # Drop test/demo owners from any people table and recompute the totals.
    for t in tables:
        rows = t.get("rows")
        if not rows:
            continue
        kept = [r for r in rows if str(r[0]) not in _EXCLUDE_NAMES]
        if len(kept) != len(rows):
            t["rows"] = kept
            ncol = len(t["columns"])
            t["total"] = ["Total"] + [sum(r[i] for r in kept) for i in range(1, ncol)]
    return tables
