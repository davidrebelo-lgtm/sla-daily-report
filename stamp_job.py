"""Scheduled write-back (run by .github/workflows/stamp.yml).

Builds the same tables as the website and stamps HubSpot, so reports 2(a)/2(o)/5(a)/5(b)
stay current even when nobody has the website open (Streamlit apps go to sleep).
Token comes from the HUBSPOT_TOKEN environment variable, filled from the GitHub secret
HUBSPOT_STAMP_TOKEN — a SEPARATE HubSpot private app (the only one with ticket write access).
"""
import os
import sys

import daily_tables
from hubspot_client import HubSpot

if __name__ == "__main__":
    tok = os.environ.get("HUBSPOT_TOKEN")
    if not tok:
        sys.exit("HUBSPOT_TOKEN is not set")
    hs = HubSpot(token=tok)
    daily_tables.build_tables(hs)
    print(daily_tables.stamp_hubspot(hs))
