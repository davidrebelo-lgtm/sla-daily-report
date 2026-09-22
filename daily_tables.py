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
_EXCLUDE_NAMES = {"Transition Demo", "Optimize Administrator"}   # dropped from every people table
# Extra per-section exclusions (by table title)
_EXCLUDE_BY_TITLE = {
    "Client Service Dashboard": {"Rohit Kapoor"},
    "Transfers (Pending Review)": {"Rohit Kapoor", "Srijan Ahuja", "Jay Suba"},
    "Account Services Dashboard": {"Rashi Tiwari"},
}


# ── Client Service Dashboard: 4b "Outstanding Tickets that are Outside SLA" (full dataset tree) ──────────────
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
_4B_NBIN_FIELDS = _4B_FIELDS + ["received_response_from_nbin"]   # + "Completed by NBIN" for 6f
# Pipelines whose "Completed" stage feeds report 6e (note: New Accounts excluded, Transfer Out included)
_6E_PIPELINE_NAMES = {"Transfer Out", "Transfer", "Add Funds", "Withdraw", "Plans"}


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


def _4b_kept_props(hs, fields):
    """Ticket property-dicts passing the report-4b outside-SLA predicate (deduped, both branches).
    Shared by 4b (Client Service Dashboard) and 6f (Client Service NBIN)."""
    now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    pl_label, st_label = _cs_label_maps(hs)

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

    kept, seen = [], set()

    def process(rows):
        for p in rows:
            tid = p.get("id")
            if tid in seen:
                continue
            seen.add(tid)
            if keep(p):
                kept.append(p)

    process(hs.search([{"propertyName": "hs_pipeline", "operator": "IN", "values": _CS_PIPELINES},
                       {"propertyName": "sla_due_date", "operator": "LT", "value": now_ms},
                       {"propertyName": "sla_due_date", "operator": "HAS_PROPERTY"}], fields))
    process(hs.search([{"propertyName": "request_type", "operator": "IN",
                        "values": ["Cancel / Correct", "Residual Transfer-In Sweep"]},
                       {"propertyName": "hs_pipeline", "operator": "IN", "values": _CS_PIPELINES}],
                      fields))
    return kept


def _4b_outside(hs):
    """Report 4b — Client Service tickets currently Outside SLA, per assigned_to."""
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for p in _4b_kept_props(hs, _4B_FIELDS):
        attr = p.get("assigned_to")
        if attr:
            nm = id_to_name.get(str(attr), str(attr))
            counts[nm] = counts.get(nm, 0) + 1
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
            "columns": ["Name", "Completed Within SLA", "Completed Outside SLA", "Outstanding Tickets that are Outside SLA"],
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
            "columns": ["Name", "Outstanding Tickets that are Outside SLA", "Pending Review — Due Today"],
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


# ── Account Services "Outstanding Tickets that are Outside SLA" — report 5b (segment-driven snapshot) ────────
_AA_COMPLETED_STAGE = "154789384"  # "Completed (Account Administration)" stage

# 5b outside-SLA segments (HubSpot active lists), matched case-insensitively by keyword.
_5B_SEGMENTS = {
    "enhanced_review":      ["outside sla", "enhanced review", "account administration"],
    "pending_action":       ["outside sla", "pending action", "account administration"],
    "transmitted":          ["outside sla", "transmitted", "account administration"],
    "pending_confirmation": ["outside sla", "pending confirmation", "account administration"],
    "preparing_paperwork":  ["outside sla", "preparing paperwork", "account administration"],  # added to 5a/5b
    "wills":                ["wills", "outside sla"],                                           # "Wills Outside SLA" segment
}


def _acct_services_outside_5b(hs):
    """Report 5b — Account Administration tickets currently Outside SLA.

    Report filter: (1 AND 2) OR (1 AND 3 AND 4)
      1 = request_type IN AA types  AND  stage != 'Completed (Account Administration)'
      2 = member of Outside-SLA segment {Enhanced Review | Pending Action | Transmitted
          | Preparing Paperwork | Wills Outside SLA}     ← Preparing Paperwork + Wills added to 5a/5b
      3 = NBIN Follow Up SLA > 15, where
          NBIN Follow Up SLA = DATEDIFF(MINUTE, notification_sent_to_assignee, sent_to_nbin__date__time)
      4 = member of Outside-SLA segment {Pending Confirmation}
    'Wills Outside SLA' = Will-Preparation tickets whose In Process / Pending Review / Final Review
    SLA is Outside SLA; clause 1 still gates it (AA request type + not Completed), so a Wills ticket
    only appears here if it also carries an AA request type. Live snapshot (no date window).
    Grouped per person by 'Assigned to' (assigned_to)."""
    seg = {}
    for key, kw in _5B_SEGMENTS.items():
        lid = _find_list_id(hs, kw)
        seg[key] = set(str(x) for x in hs.list_members(lid)) if lid else set()
    branch2 = (seg["enhanced_review"] | seg["pending_action"] | seg["transmitted"]
               | seg["preparing_paperwork"] | seg["wills"])
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


def _acct_admin_completed_outside_5e(hs):
    """Report 5e — Account Admin Tickets Completed Today (Outside SLA).

    Filter: 1 AND 2 AND 7 AND 9 AND 10 AND 11 AND (3 OR 4 OR 5 OR 6 OR 8)
      1  request_type IN AA types
      2  Assigned to != Optimize Administrator (assigned_to != 104417029)
      3-6 date_entered_<stage> is Today AND <stage> SLA (Account Administration) = 'Outside SLA'
          for stage in {Enhanced Review, Transmitted, Preparing Paperwork, Pending Action}
      7  Total Time with NBIN <= 2 days OR empty
      8  member of segment 'Account Opening Completed Today (Outside SLA)'
      9  Ticket Owner Check False/unknown = NOT(assigned_to_outside_sla == hubspot_owner_id)
      10 PM Check False/unknown          = NOT(assigned_to_outside_sla == portfolio_manager)
      11 Supervising PM Check F/unknown  = NOT(assigned_to_outside_sla == supervising_portfolio_manager)
    Grouped by 'Assigned To' = assigned_to_outside_sla. Verified live 2026-08-24 = 6 (Rohit 3, Athena 3)."""
    t0, t1 = today_bounds_ms()
    ids = set()
    for date_prop, sla_prop in _AA_STAGES:
        for r in hs.search([
            {"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
            {"propertyName": date_prop, "operator": "GTE", "value": t0},
            {"propertyName": date_prop, "operator": "LT", "value": t1},
            {"propertyName": sla_prop, "operator": "EQ", "value": "Outside SLA"},
        ], [date_prop]):
            ids.add(str(r["id"]))
    lid = _find_list_id(hs, ["account opening completed today", "outside"])   # clause 8
    if lid:
        ids.update(str(x) for x in hs.list_members(lid))
    if not ids:
        return {}
    props = hs.batch_read(list(ids),
                          ["request_type", "assigned_to", "assigned_to_outside_sla",
                           "hubspot_owner_id", "portfolio_manager", "supervising_portfolio_manager",
                           "total_time_with_nbin"])
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for p in props.values():
        if p.get("request_type") not in _ACCT_ADMIN_REQ_TYPES:              # clause 1
            continue
        if str(p.get("assigned_to")) == "104417029":                       # clause 2
            continue
        t = to_num(p.get("total_time_with_nbin"))                          # clause 7
        if t is not None and t > 2:
            continue
        aos = p.get("assigned_to_outside_sla")
        if aos and (str(aos) == str(p.get("hubspot_owner_id"))             # clause 9
                    or str(aos) == str(p.get("portfolio_manager"))         # clause 10
                    or str(aos) == str(p.get("supervising_portfolio_manager"))):  # clause 11
            continue
        if not aos:                                                        # group by Assigned To (= aos)
            continue
        nm = id_to_name.get(str(aos), str(aos))
        counts[nm] = counts.get(nm, 0) + 1
    return counts


def _acct_admin_completed_within_5f(hs):
    """Report 5f — Account Admin Tickets Completed Today (Within SLA).

    Filter: 1 AND 7 AND (2 OR 3 OR 4 OR 5 OR 6)
      1  request_type IN AA types
      2-5 date_entered_<stage> is Today AND <stage> SLA (Account Administration) = 'Within SLA'
          for stage in {Enhanced Review, Transmitted, Preparing Paperwork, Pending Action}
      6  member of segment 'Account Opening Completed Today (Within SLA)'   (the bulk of the volume)
      7  Ticket Owner Check unknown/False = NOT(assigned_to_within_sla == hubspot_owner_id)
    assigned_to_within_sla is unpopulated portal-wide, so clause 7 as-written never fires; the
    report's effective behaviour (only the two AA processors appear, advisor self-completed tickets
    drop out) is reproduced by excluding assigned_to == hubspot_owner_id. Grouped by 'Assigned to'
    = assigned_to. Target 2026-08-24 = 64 (Athena 45, Rohit 19)."""
    t0, t1 = today_bounds_ms()
    ids = set()
    for date_prop, sla_prop in _AA_STAGES:
        for r in hs.search([
            {"propertyName": "request_type", "operator": "IN", "values": _ACCT_ADMIN_REQ_TYPES},
            {"propertyName": date_prop, "operator": "GTE", "value": t0},
            {"propertyName": date_prop, "operator": "LT", "value": t1},
            {"propertyName": sla_prop, "operator": "EQ", "value": "Within SLA"},
        ], [date_prop]):
            ids.add(str(r["id"]))
    lid = _find_list_id(hs, ["account opening completed today", "within"])   # clause 6
    if lid:
        ids.update(str(x) for x in hs.list_members(lid))
    if not ids:
        return {}
    props = hs.batch_read(list(ids), ["request_type", "assigned_to", "hubspot_owner_id"])
    id_to_name, _ = hs.owner_maps()
    counts = {}
    for p in props.values():
        if p.get("request_type") not in _ACCT_ADMIN_REQ_TYPES:          # clause 1
            continue
        at = p.get("assigned_to")
        if not at:
            continue
        if str(at) == str(p.get("hubspot_owner_id")):                   # clause 7 (Ticket Owner Check)
            continue
        nm = id_to_name.get(str(at), str(at))
        counts[nm] = counts.get(nm, 0) + 1
    return counts


# ── "Amendments Required" action-item SLA (1 business day) — added into Account Services ─
# Pipeline = Account Administration; Action Item = 'Amendments Required'; SLA = 1 business day.
#   • Tickets Outside SLA  — currently in the action item > 1 business day (live snapshot).
#   • Completed Within SLA — moved THROUGH the action item (exited today) in ≤ 1 business day.
#   • Completed Outside SLA — moved through it (exited today) in > 1 business day.
# The portal does NOT populate date_entered/exited_amendments_required (0 tickets portal-wide),
# so entry/exit are read from the action_item property HISTORY. Business time = Mon–Fri, 24h/day,
# America/Toronto, no holiday calendar. Cancelled/Rejected + Optimize Administrator excluded.
_AA_PIPELINE_ID = "82170383"
_AMEND_AI = "Amendments Required"
_AMEND_SLA_SECONDS = 24 * 60 * 60          # 1 business day = 24 business hours
_TERMINAL_AI = {"Cancelled", "Rejected"}
_OPT_ADMIN_ID = "104417029"


def _business_seconds(a_ms, b_ms):
    """Elapsed business time (seconds) between two epoch-ms instants, counting only
    Mon–Fri in America/Toronto (weekends contribute zero). No holiday calendar."""
    if a_ms is None or b_ms is None or b_ms <= a_ms:
        return 0.0
    a = _dt.datetime.fromtimestamp(a_ms / 1000, TZ)
    b = _dt.datetime.fromtimestamp(b_ms / 1000, TZ)
    total, cur = 0.0, a
    while cur < b:
        nxt = cur.replace(hour=0, minute=0, second=0, microsecond=0) + _dt.timedelta(days=1)
        seg_end = min(nxt, b)
        if cur.weekday() < 5:                  # Monday=0 … Friday=4
            total += (seg_end - cur).total_seconds()
        cur = seg_end
    return total


def _history(hs, ids, props):
    """{ticket_id: {prop: [(epoch_ms, value), …] chronological}} via batch-read-with-history."""
    out = {}
    for i in range(0, len(ids), 100):
        body = {"propertiesWithHistory": props, "inputs": [{"id": x} for x in ids[i:i + 100]]}
        data = hs._req("POST", "/crm/v3/objects/tickets/batch/read", json=body)
        for r in data.get("results", []):
            ph = r.get("propertiesWithHistory", {}) or {}
            rec = {}
            for p in props:                         # HubSpot returns history newest-first
                seq = [(to_ms(h.get("timestamp")), h.get("value")) for h in reversed(ph.get(p, []))]
                rec[p] = [(t, v) for t, v in seq if t is not None]
            out[r.get("id")] = rec
    return out


def _value_at(seq, ts):
    """Value in `seq` [(ms,value)…] in effect at epoch-ms `ts` (last change at or before ts)."""
    val = None
    for t, v in seq:
        if t <= ts:
            val = v
        else:
            break
    return val


def _aa_amendments_required(hs):
    """The three Account Services columns contributed by the 'Amendments Required' 1-business-day
    action-item SLA, each tallied per assignee. Returns (within, outside, open_outside)."""
    id_to_name, _ = hs.owner_maps()
    now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    t0, t1 = today_bounds_ms()
    within, outside, open_outside = {}, {}, {}

    def _tally(bucket, aid):
        if not aid or str(aid) == _OPT_ADMIN_ID:            # skip unassigned + Optimize Administrator
            return
        nm = id_to_name.get(str(aid), str(aid))
        bucket[nm] = bucket.get(nm, 0) + 1

    # currently sitting in Amendments Required (the open / Tickets-Outside-SLA candidates)
    open_ids = [str(r["id"]) for r in hs.search(
        [{"propertyName": "hs_pipeline", "operator": "EQ", "value": _AA_PIPELINE_ID},
         {"propertyName": "action_item", "operator": "EQ", "value": _AMEND_AI}],
        ["hs_pipeline"])]
    open_set = set(open_ids)
    # AA-pipeline tickets modified today — an exit from the action item today is a modify today
    cand_ids = [str(r["id"]) for r in hs.search(
        [{"propertyName": "hs_pipeline", "operator": "EQ", "value": _AA_PIPELINE_ID},
         {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": t0}],
        ["hs_pipeline"])]

    ids = list(open_set | set(cand_ids))
    # action_item history only — 'assigned_to' is a calculated property and requesting its
    # HISTORY makes batch/read return 400, so read it as a current value instead. (Its current
    # value is the right attribution here anyway: for amendments that's the processor, i.e. Aaron.)
    hist = _history(hs, ids, ["action_item"])
    assigned_now = {str(tid): p.get("assigned_to") for tid, p in hs.batch_read(ids, ["assigned_to"]).items()}

    for tid, rec in hist.items():
        ai = rec.get("action_item", [])
        aid = assigned_now.get(str(tid))
        if not ai:
            continue
        cur_val = ai[-1][1]
        for j, (ts, val) in enumerate(ai):
            if val != _AMEND_AI:
                continue
            nxt = ai[j + 1] if j + 1 < len(ai) else None
            if nxt is None:                                  # still in the action item → open leg
                if tid in open_set and cur_val == _AMEND_AI and \
                        _business_seconds(ts, now_ms) > _AMEND_SLA_SECONDS:
                    _tally(open_outside, aid)                # whoever it is assigned to
                continue
            exit_ms, next_val = nxt
            if not (t0 <= exit_ms < t1):                     # only pass-throughs that EXITED today
                continue
            if next_val in _TERMINAL_AI or cur_val in _TERMINAL_AI:      # exclude cancelled/rejected
                continue
            dur = _business_seconds(ts, exit_ms)
            _tally(outside if dur > _AMEND_SLA_SECONDS else within, aid)  # assignee on the ticket
    return within, outside, open_outside


def _account_services(hs):
    within = _acct_admin_completed_within_5f(hs)                                                    # 5f
    outside = _acct_admin_completed_outside_5e(hs)                                                  # 5e
    open_outside = _acct_services_outside_5b(hs)                                                    # 5b
    a_within, a_outside, a_open = _aa_amendments_required(hs)          # + Amendments Required SLA leg
    for src, dst in ((a_within, within), (a_outside, outside), (a_open, open_outside)):
        for k, v in src.items():
            dst[k] = dst.get(k, 0) + v
    names = sorted(set(within) | set(outside) | set(open_outside))
    rows = [[n, within.get(n, 0), outside.get(n, 0), open_outside.get(n, 0)] for n in names]
    total = ["Total", sum(within.values()), sum(outside.values()), sum(open_outside.values())]
    return {"title": "Account Services Dashboard",
            "columns": ["Name", "Completed Within SLA", "Completed Outside SLA", "Outstanding Tickets that are Outside SLA"],
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
            "columns": ["Tickets With NBIN", "Completed Outside SLA Last 7 Days"],
            "flat_row": [with_nbin, outside]}


def _cs_completed_stage_ids(hs):
    """Stage ids labelled 'Completed' in the report-6e pipelines (resolved by pipeline name)."""
    data = hs._req("GET", "/crm/v3/pipelines/tickets").get("results", [])
    out = []
    for p in data:
        if (p.get("label") or "") in _6E_PIPELINE_NAMES:
            for s in p.get("stages", []):
                if (s.get("label") or "").strip() == "Completed":
                    out.append(str(s.get("id")))
    return out


def _client_service_nbin(hs):
    """Client Service Tickets with NBIN:  6e 'Tickets With NBIN' | 6f 'Completed Outside SLA Last 7 Days'."""
    # 6e — closed < 8 days ago (EDT), in the 6e pipelines' Completed stage, AND
    #      (Total Time with NBIN > 2 days  OR  Notification Sent to Assignee is known)
    lo = _days_ago_edt_midnight(8)
    stages = _cs_completed_stage_ids(hs)
    with_nbin = 0
    if stages:
        base = [{"propertyName": "closed_date", "operator": "GTE", "value": lo},
                {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": stages}]
        groups = [{"filters": base + [{"propertyName": "total_time_with_nbin", "operator": "GT", "value": "2"}]},
                  {"filters": base + [{"propertyName": "notification_sent_to_assignee", "operator": "HAS_PROPERTY"}]}]
        with_nbin = len({r["id"] for r in hs.search_groups(groups, ["request_type"])})
    # 6f — the 4b outside-SLA tickets that are still with NBIN
    #      (Sent to NBIN known AND Completed by NBIN unknown)
    outside = 0
    for p in _4b_kept_props(hs, _4B_NBIN_FIELDS):
        if p.get("sent_to_nbin__date__time") and not p.get("received_response_from_nbin"):
            outside += 1
    return {"title": "Client Service Tickets with NBIN",
            "columns": ["Tickets With NBIN", "Completed Outside SLA Last 7 Days"],
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
_ADVISOR_COLS = ["Name", "Tickets Completed Within SLA", "Tickets Completed Outside SLA", "Outstanding Tickets that are Outside SLA"]


def _people_tbl(title, w, o, oo, cols):
    names = sorted(set(w) | set(o) | set(oo))
    rows = [[n, w.get(n, 0), o.get(n, 0), oo.get(n, 0)] for n in names]
    return {"title": title, "columns": cols, "rows": rows,
            "total": ["Total", sum(w.values()), sum(o.values()), sum(oo.values())]}


_2B_ASSIGNEES = {"Batuhan Karabay", "Christian Alvarez", "Ryan Connon", "Shivani Shaurya",
                 "Phil Kolanowski", "Andrew Kirkham", "Ali Vahedi", "Adam Goldband", "Gabriel Tan"}
_2B_OWNER_EXCLUDE = {"Stephanie Hunter", "Daniel Willett"}


def _advisor_open_pending_action_2b(hs):
    """Report 2b — Advisor Support Pending Action tickets currently Outside SLA.

    Members of segment 'Outside SLA - Pending Action (Support Tickets)' AND:
      2  pipeline Support Ticket, stage != Closed
      3  owner not Stephanie Hunter / Daniel Willett (empty owner OK); submitted_by not Daniel Willett
      4  Assigned to in the Advisor Support roster
      5  Ticket Opened After 5:30 is before yesterday, or unknown
    Grouped by Assigned to (assigned_to). The bare segment count previously counted tickets the
    report excludes (e.g. a ticket owned by Daniel Willett) — this applies the full 2b filter."""
    seg = hs.sla_segments().get("Pending Action")
    if not seg:
        return {}
    _, closed = hs.support_ids()
    id_to_name, _ = hs.owner_maps()
    yest = _dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - _dt.timedelta(days=1)
    yest_ms = int(yest.astimezone(_dt.timezone.utc).timestamp() * 1000)
    props = hs.batch_read(hs.list_members(seg),
                          ["action_item", "hs_pipeline_stage", "assigned_to", "hubspot_owner_id",
                           "request_submitted_by", "ticket_opened_after_530"])
    counts = {}
    for p in props.values():
        if p.get("action_item") != "Pending Action":                          # filter 1
            continue
        if str(p.get("hs_pipeline_stage")) == str(closed):                     # filter 2
            continue
        owner = p.get("hubspot_owner_id")
        if owner and id_to_name.get(str(owner)) in _2B_OWNER_EXCLUDE:          # filter 3a
            continue
        if "daniel willett" in (p.get("request_submitted_by") or "").lower():  # filter 3b
            continue
        a = p.get("assigned_to")
        if not a:
            continue
        nm = id_to_name.get(str(a), str(a))
        if nm not in _2B_ASSIGNEES:                                            # filter 4
            continue
        oa = to_ms(p.get("ticket_opened_after_530"))                          # filter 5
        if oa is not None and oa >= yest_ms:
            continue
        counts[nm] = counts.get(nm, 0) + 1
    return counts


def _advisor_pending_action(hs):
    w_pa = reports._sum_per_person(hs, [reports._today_pending_action(hs, True)])
    o_pa = reports._sum_per_person(hs, [reports._today_pending_action(hs, False)])
    open_pa = _advisor_open_pending_action_2b(hs)
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
         ["Tickets With NBIN", "Completed Outside SLA Last 7 Days"]),
        ("Client Service Tickets with NBIN", _client_service_nbin,
         ["Tickets With NBIN", "Completed Outside SLA Last 7 Days"]),
        ("Account Services Dashboard", _account_services,
         ["Name", "Completed Within SLA", "Completed Outside SLA", "Outstanding Tickets that are Outside SLA"]),
        ("Transfers (Pending Review)", _transfers,
         ["Name", "Outstanding Tickets that are Outside SLA", "Pending Review — Due Today"]),
        ("Client Service Dashboard", _client_service_dashboard,
         ["Name", "Completed Within SLA", "Completed Outside SLA", "Outstanding Tickets that are Outside SLA"]),
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
        drop = _EXCLUDE_NAMES | _EXCLUDE_BY_TITLE.get(t.get("title", ""), set())
        kept = [r for r in rows if str(r[0]) not in drop]
        if len(kept) != len(rows):
            t["rows"] = kept
            ncol = len(t["columns"])
            t["total"] = ["Total"] + [sum(r[i] for r in kept) for i in range(1, ncol)]
    return tables
