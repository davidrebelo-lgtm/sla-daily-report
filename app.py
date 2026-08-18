"""Daily SLA Report — standalone live dashboard (Streamlit).

Renders every table in the daily SLA email, pulled LIVE from HubSpot, at a URL anyone
can open. Independent of any other app. Token lives in Streamlit Secrets (HUBSPOT_TOKEN),
never in the code.
"""
import datetime as dt
import os

import streamlit as st

import daily_tables

st.set_page_config(page_title="Daily SLA Report", page_icon="◆", layout="wide")

NAVY = "#2B3A4E"
LIGHT = "#EEF2F6"
MUTED = "#5B6B7B"
STRIPE = "#F7F9FB"


def _token():
    tok = os.environ.get("HUBSPOT_TOKEN")
    if not tok:
        try:
            tok = st.secrets["HUBSPOT_TOKEN"]
        except Exception:
            tok = None
    return tok


@st.cache_data(ttl=300, show_spinner="Pulling live data from HubSpot …")
def _load(_tok_tail):
    from hubspot_client import HubSpot
    hs = HubSpot(token=_token())
    return daily_tables.build_tables(hs), dt.datetime.now(dt.timezone.utc)


def _table_html(t):
    cols = t["columns"]
    first_left = ("Name" in cols)
    align = ["left"] + ["center"] * (len(cols) - 1) if first_left else ["center"] * len(cols)
    head = "".join(f'<th style="padding:7px 10px;color:#fff;font-size:12px;text-align:{align[i]}">{c}</th>'
                   for i, c in enumerate(cols))
    body = ""
    rows = []
    if t.get("flat_row") is not None:
        rows = [t["flat_row"]]
    else:
        rows = t.get("rows", [])
    for ri, r in enumerate(rows):
        bg = STRIPE if ri % 2 else "#fff"
        tds = "".join(f'<td style="padding:6px 10px;font-size:13px;color:{NAVY};text-align:{align[i]};'
                      f'border-bottom:1px solid {LIGHT}">{v}</td>' for i, v in enumerate(r))
        body += f'<tr style="background:{bg}">{tds}</tr>'
    total = t.get("total")
    if total is not None:
        tds = "".join(f'<td style="padding:7px 10px;font-size:13px;font-weight:700;color:{NAVY};'
                      f'text-align:{align[i]};background:{LIGHT};border-top:2px solid {NAVY}">{v}</td>'
                      for i, v in enumerate(total))
        body += f"<tr>{tds}</tr>"
    note = (f'<div style="color:{MUTED};font-style:italic;font-size:12px;margin-top:4px">{t["note"]}</div>'
            if t.get("note") else "")
    return (f'<div style="font-weight:700;color:{NAVY};font-size:16px;margin:18px 0 6px">{t["title"]}</div>'
            f'<table style="border-collapse:collapse;width:100%;border:1px solid {LIGHT}">'
            f'<tr style="background:{NAVY}">{head}</tr>{body}</table>{note}')


tok = _token()
if not tok:
    st.error("No HUBSPOT_TOKEN configured. Add it under the app's **Settings → Secrets** "
             'as  HUBSPOT_TOKEN = "pat-na1-…"  (read scopes: tickets, owners, lists).')
    st.stop()

st.markdown(f'<h1 style="color:{NAVY};margin-bottom:0">Daily SLA Report</h1>'
            f'<div style="color:{MUTED}">Tickets Outside SLA</div>', unsafe_allow_html=True)

try:
    tables, when = _load(tok[-6:])
    local = when.astimezone()  # server tz; informational
    st.caption(f"Live from HubSpot · pulled {when.strftime('%Y-%m-%d %H:%M UTC')} · refreshes every 5 min")
    for t in tables:
        st.markdown(_table_html(t), unsafe_allow_html=True)
except Exception as e:
    st.error(f"Could not load the report: {e}")

if st.button("↻ Refresh now"):
    st.cache_data.clear()
    st.rerun()
