"""HubSpot client for the Claude ops dashboards.

Each board is a union of several CRM reports off the "New SLA Enhancement
Dashboard". We reproduce each underlying report's filters against the CRM Search
API, union the resulting tickets by id, attribute each to its "Assigned to" owner,
and count per person. Filters HubSpot Search can't express (calculated properties,
report formula fields) are applied client-side over the returned rows.

Property internal names were resolved from the ticket schema on 2026-07-06.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

BASE = "https://api.hubapi.com"
TZ = ZoneInfo("America/Toronto")

# --- test-ticket filter -----------------------------------------------------
# Test tickets use a fake client name of the form "Client <number>" in the subject:
# "Client Seven", "Client One", "Miao Client 1", "Khoi Client 3", "Client Fake Seven",
# "Client Thirty-Four", etc. Real tickets with the word "Client" (e.g. "Client Consent
# Form", "Credit Client Account", "Client Passed Away") are NOT matched because there
# "Client" is never followed by a number word/digit. Filtered out of every count.
_TEST_SUBJECT_RE = re.compile(
    r"client\s+(fake\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred)\b", re.I)
# Test-family "Corp" entities (e.g. "KYC Update - Miao Corp"). Matches only the known test
# families, so real corporations ("Della Malva ... Professional Corp.", "PCOIT INC") are untouched.
_TEST_FAMILY_RE = re.compile(r"\b(miao|khoi|nicholas|daric)\s+corp\b", re.I)


def is_test_subject(subject) -> bool:
    return bool(subject and (_TEST_SUBJECT_RE.search(subject) or _TEST_FAMILY_RE.search(subject)))

# --- ticket property internal names (resolved from the schema) --------------
P = {
    "pipeline": "hs_pipeline",
    "stage": "hs_pipeline_stage",
    "action_item": "action_item",
    "action_item_updated": "action_item_updated",     # date, calc=False
    "owner": "hubspot_owner_id",
    "assigned_to": "assigned_to",                      # owner-ref enum (dimension)
    "submitted_by": "request_submitted_by",            # string
    "assigned_to_processing": "assigned_to_processing",
    "assigned_to_support_ticket": "assigned_to_support_ticket",
    "assigned_to_final_review": "assigned_to_final_review",
    "in_process_reason": "support_ticket__in_process_reason",
    "date_entered_in_process": "date_entered_in_process_support_ticket",           # calc=False
    "date_entered_in_review": "date_entered_in_review_pending_action",             # calc=False; label "Date entered In Review (Support Ticket)"
    "date_exited_in_process": "date_exited_in_process_support_ticket",             # calc=False
    "date_exited_pending_conf": "date_exited_pending_confirmation_support_ticket",  # calc=False
    "date_entered_pending_conf": "date_entered_pending_confirmation_support_ticket",  # calc=False
    "date_entered_pending_action": "date_entered_in_process_pending_action",       # calc=False
    "ttfr": "time_to_first_agent_reply",               # number, calc=False (minutes)
    "create_date": "createdate",
    # NOTE: the reports' "Total Time in <stage>" is a DATEDIFF formula (entered -> exited
    # that stage), NOT a stored property — the properties total_time_in_process_support_ticket
    # and total_time_in_pending_confirmation_support_ticket DO NOT EXIST. All three legs now
    # compute the SLA time via reports._datediff_minutes.
    "first_agent_response": "hs_first_agent_message_sent_at",
}

SUPPORT_PIPELINE_LABEL = "Support Ticket"
CLOSED_STAGE_LABEL = "Closed"   # the stage label in the Support pipeline is just "Closed"


class HubSpot:
    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("HUBSPOT_TOKEN")
        if not self.token:
            raise RuntimeError("No HUBSPOT_TOKEN set.")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {self.token}",
                               "Content-Type": "application/json"})
        self._owner_cache = None
        self._pipeline_cache = None
        self._seg_cache = None
        self._test_contact_cache = None

    # -- test-contact filter -------------------------------------------------
    def test_contact_ids(self):
        """Contact ids of fake test clients: internal @ofg.com emails tagged '+client'
        (e.g. khoi.hoang+client3@ofg.com, client2@ofg.com) or last name 'Client <n>'.
        Cached once per run; best-effort (returns empty on any error so the report never breaks)."""
        if self._test_contact_cache is not None:
            return self._test_contact_cache
        ids = set()
        try:
            after = None
            while True:
                body = {"filterGroups": [{"filters": [
                            {"propertyName": "email", "operator": "CONTAINS_TOKEN", "value": "ofg"}]}],
                        "properties": ["email", "lastname"], "limit": 100,
                        "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
                if after:
                    body["after"] = after
                data = self._req("POST", "/crm/v3/objects/contacts/search", json=body)
                for c in data.get("results", []):
                    p = c.get("properties", {})
                    email = (p.get("email") or "").lower()
                    last = (p.get("lastname") or "").strip()
                    if ("+client" in email or re.match(r"^client\d*@", email)
                            or re.match(r"^client\s+\d+$", last, re.I)):
                        ids.add(str(c["id"]))
                after = data.get("paging", {}).get("next", {}).get("after")
                if not after or len(ids) > 10000:
                    break
        except Exception as e:
            print(f"[dash][WARN] test_contact_ids failed ({e}); subject filter still applies", file=sys.stderr)
            return set()
        self._test_contact_cache = ids
        return ids

    def _tickets_with_test_contact(self, ids):
        """Subset of `ids` (ticket ids) associated with a test contact. Best-effort."""
        test = self.test_contact_ids()
        if not test or not ids:
            return set()
        bad = set()
        try:
            sids = [str(x) for x in ids]
            for i in range(0, len(sids), 100):
                data = self._req("POST", "/crm/v4/associations/tickets/contacts/batch/read",
                                 json={"inputs": [{"id": x} for x in sids[i:i + 100]]})
                for r in data.get("results", []):
                    frm = str(r.get("from", {}).get("id"))
                    for a in r.get("to", []):
                        if str(a.get("toObjectId") or a.get("id") or "") in test:
                            bad.add(frm)
                            break
        except Exception as e:
            print(f"[dash][WARN] contact-association filter failed ({e}); subject filter still applies",
                  file=sys.stderr)
            return set()
        return bad

    def _req(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, f"{BASE}{path}", timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    # -- metadata -----------------------------------------------------------
    def owner_maps(self):
        """(id_to_name, name_to_id) from the owners endpoint. assigned_to and
        hubspot_owner_id both store owner ids."""
        if self._owner_cache:
            return self._owner_cache
        id_to_name, name_to_id = {}, {}
        # Fetch active owners AND archived (deactivated) owners, so someone who
        # completed tickets in the window and was later deactivated (e.g. Ardinela
        # Hoxha, Philip Swan) still resolves to a name instead of a raw owner id.
        for archived in ("false", "true"):
            after = None
            while True:
                params = {"limit": 100, "archived": archived}
                if after:
                    params["after"] = after
                data = self._req("GET", "/crm/v3/owners", params=params)
                for o in data.get("results", []):
                    oid = str(o.get("id"))
                    name = (f"{o.get('firstName') or ''} {o.get('lastName') or ''}".strip()
                            or o.get("email") or oid)
                    id_to_name.setdefault(oid, name)          # active name wins on any clash
                    name_to_id.setdefault(name.casefold(), oid)
                after = data.get("paging", {}).get("next", {}).get("after")
                if not after:
                    break
        self._owner_cache = (id_to_name, name_to_id)
        return self._owner_cache

    def support_ids(self):
        """(support_pipeline_id, closed_stage_id)."""
        if self._pipeline_cache:
            return self._pipeline_cache
        data = self._req("GET", "/crm/v3/pipelines/tickets")
        pid, closed = None, None
        for pl in data.get("results", []):
            if pl["label"] == SUPPORT_PIPELINE_LABEL:
                pid = pl["id"]
                for st in pl.get("stages", []):
                    if st["label"] == CLOSED_STAGE_LABEL:
                        closed = st["id"]
        self._pipeline_cache = (pid, closed)
        return self._pipeline_cache

    # -- generic search -----------------------------------------------------
    def search(self, filters: list, props: list) -> list:
        """Tickets matching the AND of `filters` (one filter group)."""
        return self.search_groups([{"filters": filters}], props)

    def search_groups(self, filter_groups: list, props: list) -> list:
        """Tickets matching ANY filter group (groups are OR'd), each group's filters
        AND'd. Always includes assigned_to + id. Paginates fully.

        A stable ``sorts`` (hs_object_id ascending) is REQUIRED for correct paging:
        without it HubSpot's page order is undefined and, because these tickets are
        being modified live, records can shift between page fetches and be duplicated
        or skipped — the source of intermittent miscounts. With a fixed sort the
        cursor is deterministic. We also guard the Search API's hard 10k-result cap
        so silent truncation surfaces in the logs instead of quietly undercounting."""
        out, after, seen = [], None, set()
        want = list({*props, P["assigned_to"], "hs_object_id", "subject"})
        while True:
            body = {"filterGroups": filter_groups, "properties": want, "limit": 100,
                    "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}]}
            if after:
                body["after"] = after
            data = self._req("POST", "/crm/v3/objects/tickets/search", json=body)
            for t in data.get("results", []):
                tid = t.get("id")
                if tid in seen:                       # belt-and-suspenders de-dupe
                    continue
                seen.add(tid)
                row = dict(t.get("properties", {}))
                if is_test_subject(row.get("subject")):   # drop test tickets globally
                    continue
                row["id"] = tid
                out.append(row)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
            if len(out) >= 10000:                     # HubSpot Search hard cap
                print("[dash][WARN] search hit the 10,000-result cap — results "
                      f"truncated; filters={filter_groups}", file=sys.stderr)
                break
        bad = self._tickets_with_test_contact([r["id"] for r in out])   # drop test-contact tickets
        if bad:
            out = [r for r in out if r["id"] not in bad]
        return out

    # -- segments / lists ---------------------------------------------------
    def sla_segments(self) -> dict:
        """Resolve the 'Outside SLA - <stage> (Support Tickets)' ticket segments
        by name -> {stage: listId}. Looked up by name so it survives list-id
        changes. Cached per client."""
        if self._seg_cache is not None:
            return self._seg_cache
        data = self._req("POST", "/crm/v3/lists/search", json={"query": "Outside SLA", "count": 100})
        seg = {}
        for l in data.get("lists", []):
            name = l.get("name") or ""
            if l.get("objectTypeId") == "0-5" and "Outside SLA" in name and "Support Tickets" in name:
                for stage in ("Pending Action", "In Process", "Pending Confirmation"):
                    if stage in name:
                        seg[stage] = l.get("listId")
        self._seg_cache = seg
        return seg

    def list_members(self, list_id) -> list:
        """All ticket record ids in a list/segment (paginated)."""
        ids, after = [], None
        while True:
            path = f"/crm/v3/lists/{list_id}/memberships?limit=250" + (f"&after={after}" if after else "")
            data = self._req("GET", path)
            ids += [r["recordId"] for r in data.get("results", [])]
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return ids

    def batch_read(self, ids: list, props: list) -> dict:
        """{ticket_id: {prop: value}} via batch read (100 ids/call). Test tickets dropped."""
        out = {}
        want = list({*props, "subject"})
        for i in range(0, len(ids), 100):
            body = {"properties": want, "inputs": [{"id": x} for x in ids[i:i + 100]]}
            data = self._req("POST", "/crm/v3/objects/tickets/batch/read", json=body)
            for r in data.get("results", []):
                pr = r.get("properties", {})
                if is_test_subject(pr.get("subject")):     # drop test tickets globally
                    continue
                out[r["id"]] = pr
        for tid in self._tickets_with_test_contact(list(out.keys())):   # drop test-contact tickets
            out.pop(tid, None)
        return out

    def action_item_last_changed(self, ids: list) -> dict:
        """{ticket_id: epoch_ms of the most recent action_item change} via the
        batch-read-with-history API (100 ids/call). Reproduces HubSpot's
        'Action Item (not) updated in the last N days' property-history filter."""
        out = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            body = {"propertiesWithHistory": ["action_item"],
                    "inputs": [{"id": x} for x in chunk]}
            data = self._req("POST", "/crm/v3/objects/tickets/batch/read", json=body)
            for r in data.get("results", []):
                hist = r.get("propertiesWithHistory", {}).get("action_item", [])
                if hist:  # history is newest-first
                    out[r.get("id")] = to_ms(hist[0].get("timestamp"))
        return out


# --- date helpers -----------------------------------------------------------
def start_of_today_ms() -> int:
    d = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.astimezone(dt.timezone.utc).timestamp() * 1000)


def days_ago_ms(days: int) -> int:
    d = dt.datetime.now(TZ) - dt.timedelta(days=days)
    return int(d.astimezone(dt.timezone.utc).timestamp() * 1000)


def today_bounds_ms():
    """(start_of_today, start_of_tomorrow) in epoch ms, America/Toronto — for
    '<date> is Today' filters (value >= start AND value < end)."""
    start = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return (int(start.astimezone(dt.timezone.utc).timestamp() * 1000),
            int(end.astimezone(dt.timezone.utc).timestamp() * 1000))


def to_ms(v):
    """Normalise a HubSpot datetime value (epoch-ms string or ISO) to epoch ms."""
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None


def to_num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
