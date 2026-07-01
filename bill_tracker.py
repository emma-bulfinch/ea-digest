"""
EA Legislative Tracker
-----------------------
Unlike digest.py (which only catches things reporters happen to write about),
this script queries actual legislative data via the Open States API
(https://openstates.org — free, covers all 50 states) and diffs it against
what we saw last run, so we catch:

  - Brand-new bill filings matching our keyword list
  - Committee action (referred, reported out, amended, hearing scheduled)
  - Floor votes / passage / signature — flagged as "significant" and
    triggers an immediate email rather than waiting for the daily digest
  - Bills related/similar to ones we're already tracking
  - Federal bills (House/Senate) via Congress.gov, filtered by keyword match
    against titles since Congress.gov has no full-text search endpoint

State is persisted in data/bill_state.json, which this script updates and
the GitHub Actions workflow commits back to the repo after every run.
"""

import requests
import json
import os
import sys
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# ── Configuration ──────────────────────────────────────────────────────────
SENDER = "emma@thinkjet.io"
RECIPIENTS = [
    "jefferson@thinkjet.io",
    "brianna@thinkjet.io",
    "ayah@thinkjet.io",
    "emma@thinkjet.io",
]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
OPENSTATES_API_KEY = os.environ["OPENSTATES_API_KEY"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional but recommended

STATE_STORE_PATH = "data/bill_state.json"
OPENSTATES_BASE = "https://v3.openstates.org"
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY")
CONGR
