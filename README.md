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

Nothing is emailed or written back to HubSpot — this is read-only reporting.
