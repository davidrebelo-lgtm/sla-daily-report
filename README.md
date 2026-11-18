# SLA Daily Report — live Streamlit dashboard

Reproduces every table in the daily SLA email, pulled **live** from HubSpot, at a URL anyone can open.
Independent of any other dashboard. The HubSpot token lives in Streamlit **Secrets**, never in the code.

## Files
- `app.py` — the Streamlit app (renders the report)
- `daily_tables.py` — builds every table live from HubSpot (validated logic)
- `reports.py` — Advisor Support boards
- `hubspot_client.py` — HubSpot API client
- `requirements.txt` — dependencies (streamlit, requests)

## Deploy (Streamlit Community Cloud)
1. Push these files to the repo root.
2. On https://share.streamlit.io → **New app** → point at this repo, main file `app.py`.
3. In the app's **Settings → Secrets**, add:
       HUBSPOT_TOKEN = "pat-na1-..."
   The token is a HubSpot Private App token with read scopes:
   `crm.objects.tickets.read`, `crm.objects.owners.read`, `crm.lists.read`.
4. The app pulls live on load and caches for 5 minutes; use **↻ Refresh now** to force a pull.

## HubSpot write-back (keeps HubSpot sections 2 and 5 matching this page)
A scheduled job (`stamp_job.py`, run by `.github/workflows/stamp.yml` every 5 min on weekdays
~7 AM–8 PM Toronto) builds the same tables as this page and labels tickets in HubSpot (`stamp.py`):

Outstanding Outside SLA (reports 2(a), 2(c), 2(d), 2(o), 5(a), 5(b)):
  `sla_report_open_outside` ("Advisor Support"/"Account Services"), `sla_report_counted_for` (name),
  `sla_report_open_step` (Action Item when counted), `sla_report_last_stamped`
Advisor Support (Pending Action) table — Outstanding (report 2(b)):
  `sla_report_pa_open_by` (name)
Advisor Support completed today (reports 2(k)–2(n), 2(p), 2(q), + PA today):
  `sla_today_date` (date) and per step `sla_today_{pa,ip,pc,cr}_result` ("Within"/"Outside") +
  `sla_today_{pa,ip,pc,cr}_by` (name). Only count when SLA Today Date is today.

The HubSpot reports filter on these, so they always match. Nothing else is written.
The website itself stays read-only; the job uses its own token, repo secret `HUBSPOT_STAMP_TOKEN`
(scopes: tickets read+write, owners read, lists read).
