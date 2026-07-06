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
ALERT_LOG_PATH = "data/alert_log.json"
OPENSTATES_BASE = "https://v3.openstates.org"
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY")
CONGRESS_BASE = "https://api.congress.gov/v3"
CURRENT_CONGRESS = 119  # 119th Congress covers 2025-2026; bump to 120 starting Jan 2027

STATES = [
    "Massachusetts", "New Jersey", "New York", "Pennsylvania",
    "Maryland", "Virginia", "Utah", "Colorado",
    "Texas", "Washington", "Illinois", "Ohio",
]

# Keyword list used to locally filter each state's recently-updated bills
# (title + abstract text). Add/remove terms here as EA's priorities shift —
# no code changes needed elsewhere.
SEARCH_TERMS = [
    "electric vehicle charging",
    "electric vehicle supply equipment",
    "EVSE",
    "make-ready parking",
    "NEVI",
    "electric vehicle infrastructure",
    "charging station",
    "kilowatt-hour tax",
    "vehicle miles traveled fee",
    "road usage charge",
    "EV registration fee",
    "battery energy storage",
    "electric vehicle accessibility",
]

# Action-description keyword buckets used to classify how big a deal an update is.
SIGNIFICANT_KEYWORDS = [
    "signed by governor", "signed into law", "enacted", "adopted",
    "passed both", "concurred in", "sent to governor", "vetoed",
    "public act", "chaptered", "substituted by", "substituted for",
]
COMMITTEE_KEYWORDS = [
    "referred to committee", "reported out of committee", "reported favorably",
    "committee substitute", "hearing scheduled", "amended", "reported with amendments",
    "passed committee", "recommended for passage",
]


def load_state() -> dict:
    if os.path.exists(STATE_STORE_PATH):
        with open(STATE_STORE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_STORE_PATH), exist_ok=True)
    with open(STATE_STORE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def load_alert_log() -> list[dict]:
    if os.path.exists(ALERT_LOG_PATH):
        with open(ALERT_LOG_PATH) as f:
            return json.load(f)
    return []


def save_alert_log(log: list[dict]) -> None:
    # Trim anything older than 30 days so this file doesn't grow forever.
    cutoff = datetime.utcnow() - timedelta(days=30)
    trimmed = [
        entry for entry in log
        if datetime.fromisoformat(entry["logged_at"]) > cutoff
    ]
    os.makedirs(os.path.dirname(ALERT_LOG_PATH), exist_ok=True)
    with open(ALERT_LOG_PATH, "w") as f:
        json.dump(trimmed, f, indent=2, sort_keys=True)


def append_alerts_to_log(notable_new: list[dict], notable_changes: list[dict]) -> None:
    """
    Records every significant/committee-level event with a timestamp, so
    digest.py can later pull "what happened in the last 24 hours" for the
    daily email — this is what makes legislative alerts show up there too,
    not just in the separate real-time alert emails.
    """
    log = load_alert_log()
    now = datetime.utcnow().isoformat()

    for b in notable_new:
        log.append({
            "logged_at": now,
            "event": "new_filing",
            "jurisdiction": b["jurisdiction"],
            "identifier": b["identifier"],
            "title": b["title"],
            "detail": b["latest_action"],
            "level": b["level"],
            "url": b["url"],
        })
    for b in notable_changes:
        log.append({
            "logged_at": now,
            "event": "status_change",
            "jurisdiction": b["jurisdiction"],
            "identifier": b["identifier"],
            "title": b["title"],
            "detail": f"{b['previous_action']} -> {b['latest_action']}",
            "level": b["level"],
            "url": b["url"],
        })

    save_alert_log(log)


def openstates_recent(jurisdiction: str, per_page: int = 20) -> list[dict]:
    """
    One request per state instead of one-per-keyword. Pulls the most recently
    updated bills (with abstracts, so we can keyword-match against more than
    just the title) and we filter locally — this is what keeps us well under
    Open States' free-tier limits (1 req/sec, 500/day) even running hourly.
    """
    url = f"{OPENSTATES_BASE}/bills"
    params = {
        "jurisdiction": jurisdiction,
        "sort": "updated_desc",
        "include": "actions",
        "per_page": per_page,
        "apikey": OPENSTATES_API_KEY,
    }
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json().get("results", [])
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429 and attempt == 1:
                print(f"  Rate limited on {jurisdiction}, waiting 5s and retrying once...", file=sys.stderr)
                time.sleep(5)
                continue
            print(f"  WARNING: Open States query failed ({jurisdiction}): {e}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"  WARNING: Open States query failed ({jurisdiction}): {e}", file=sys.stderr)
            return []
    return []


def bill_matches_keywords(bill: dict) -> bool:
    blob = (bill.get("title") or "").lower()
    for abstract in bill.get("abstracts") or []:
        blob += " " + (abstract.get("abstract") or "").lower()
    return any(term.lower() in blob for term in SEARCH_TERMS)


def classify_action(description: str) -> str:
    d = (description or "").lower()
    if any(k in d for k in SIGNIFICANT_KEYWORDS):
        return "significant"
    if any(k in d for k in COMMITTEE_KEYWORDS):
        return "committee"
    return "minor"


def double_check_with_claude(bill: dict) -> str | None:
    """
    Verification pass: ask Claude to summarize the bill using ONLY the
    structured data we actually pulled, explicitly forbidding invented
    details (bill numbers, sponsors, outcomes not present in the data).
    This is the "double check" step before anything goes in an alert email.
    """
    if not ANTHROPIC_API_KEY:
        return None
    prompt = (
        "You are fact-checking a legislative alert before it goes to a client. "
        "Using ONLY the structured data below, write a 2-3 sentence plain-English "
        "summary of what this bill does and its current status. Do not invent bill "
        "numbers, sponsors, dates, or outcomes that are not present in the data. "
        "If the data is too sparse to describe what the bill actually does, say so "
        "explicitly rather than guessing.\n\n"
        f"Bill data: {json.dumps(bill, indent=2)[:4000]}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json().get("content", [])
        return "".join(block.get("text", "") for block in content).strip() or None
    except Exception as e:
        print(f"  WARNING: Claude double-check failed: {e}", file=sys.stderr)
        return None


def find_similar_bills(bill: dict, all_results: list[dict]) -> list[str]:
    """Other bills in the same state/session sharing a subject tag with this one."""
    subjects = set(bill.get("subject") or [])
    if not subjects:
        return []
    similar = []
    for other in all_results:
        if other["id"] == bill["id"]:
            continue
        if subjects & set(other.get("subject") or []):
            similar.append(f"{other.get('identifier', '?')}: {other.get('title', '')}")
    return similar[:3]


def congress_recent_bills(hours_back: int = 6) -> list[dict]:
    """
    Congress.gov's API has no full-text keyword search endpoint (that only
    exists on the website), so instead we pull bills updated in the lookback
    window and filter by title keyword match locally — same principle as the
    state search, adapted to what the API actually offers.
    """
    if not CONGRESS_API_KEY:
        return []
    from_dt = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{CONGRESS_BASE}/bill/{CURRENT_CONGRESS}"
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "sort": "updateDate+desc",
        "fromDateTime": from_dt,
        "limit": 250,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        bills = r.json().get("bills", [])
    except Exception as e:
        print(f"  WARNING: Congress.gov query failed: {e}", file=sys.stderr)
        return []

    matches = []
    for b in bills:
        title = (b.get("title") or "").lower()
        if any(term.lower() in title for term in SEARCH_TERMS):
            matches.append(b)
    return matches


def federal_record(bill: dict) -> dict:
    bill_type = (bill.get("type") or "").upper()
    number = bill.get("number", "")
    congress = bill.get("congress", CURRENT_CONGRESS)
    latest_action = bill.get("latestAction", {}) or {}
    chamber = "house-bill" if bill_type.startswith("H") else "senate-bill"
    return {
        "identifier": f"{bill_type}{number}",
        "title": bill.get("title", ""),
        "jurisdiction": "U.S. Congress",
        "latest_action": latest_action.get("text", ""),
        "latest_date": latest_action.get("actionDate", ""),
        "url": f"https://www.congress.gov/bill/{congress}th-congress/{chamber}/{number}",
    }


def scan_federal_bills(state_store: dict) -> tuple[list[dict], list[dict]]:
    new_filings = []
    status_changes = []

    if not CONGRESS_API_KEY:
        return new_filings, status_changes

    for bill in congress_recent_bills():
        bill_type = (bill.get("type") or "").upper()
        number = bill.get("number", "")
        congress = bill.get("congress", CURRENT_CONGRESS)
        key = f"congress-{congress}-{bill_type}-{number}"
        record = federal_record(bill)

        prior = state_store.get(key)
        if prior is None:
            summary = double_check_with_claude(record)
            new_filings.append({**record, "similar": [], "verified_summary": summary,
                                 "level": classify_action(record["latest_action"])})
        elif prior.get("latest_date") != record["latest_date"]:
            summary = double_check_with_claude(record)
            status_changes.append({**record, "previous_action": prior.get("latest_action", ""),
                                    "verified_summary": summary,
                                    "level": classify_action(record["latest_action"])})

        state_store[key] = record

    return new_filings, status_changes


def scan_state_bills(state_store: dict) -> tuple[list[dict], list[dict]]:
    new_filings = []
    status_changes = []

    for state_name in STATES:
        all_results = openstates_recent(state_name)
        jurisdiction_results = [b for b in all_results if bill_matches_keywords(b)]
        time.sleep(2.0)  # extra margin under the 1 request/sec free-tier limit

        for bill in jurisdiction_results:
            bill_id = bill["id"]
            actions = bill.get("actions") or []
            latest_action = actions[-1] if actions else {}
            latest_desc = latest_action.get("description", "")
            latest_date = latest_action.get("date", "")

            record = {
                "identifier": bill.get("identifier", "unknown"),
                "title": bill.get("title", ""),
                "jurisdiction": state_name,
                "latest_action": latest_desc,
                "latest_date": latest_date,
                "url": bill.get("openstates_url") or (bill.get("sources") or [{}])[0].get("url", ""),
            }

            prior = state_store.get(bill_id)

            if prior is None:
                similar = find_similar_bills(bill, all_results)
                summary = double_check_with_claude(record)
                new_filings.append({
                    **record,
                    "similar": similar,
                    "verified_summary": summary,
                    "level": classify_action(latest_desc),
                })
            elif prior.get("latest_date") != latest_date:
                summary = double_check_with_claude(record)
                status_changes.append({
                    **record,
                    "previous_action": prior.get("latest_action", ""),
                    "verified_summary": summary,
                    "level": classify_action(latest_desc),
                })

            state_store[bill_id] = record

    return new_filings, status_changes


def build_alert_email(new_filings: list[dict], status_changes: list[dict]) -> str:
    lines = ["Team — legislative tracker update.", ""]

    urgent = [b for b in status_changes if b["level"] == "significant"]
    if urgent:
        lines.append("SIGNIFICANT ACTION")
        for b in urgent:
            lines.append(f"[{b['jurisdiction']}] {b['identifier']} — {b['title']}")
            lines.append(f"  Previous: {b['previous_action']}")
            lines.append(f"  Now: {b['latest_action']} ({b['latest_date']})")
            if b.get("verified_summary"):
                lines.append(f"  Summary: {b['verified_summary']}")
            lines.append(f"  Source: {b['url']}")
            lines.append("")

    # Only surface new filings that are already significant or under committee
    # review — routine/minor matches are tracked (visible in the daily digest
    # snapshot) but don't clutter this alert email.
    notable_new = [b for b in new_filings if b["level"] in ("significant", "committee")]
    if notable_new:
        lines.append("NEWLY FILED")
        for b in notable_new:
            lines.append(f"[{b['jurisdiction']}] {b['identifier']} — {b['title']}")
            lines.append(f"  Status: {b['latest_action']} ({b['latest_date']})")
            if b.get("similar"):
                lines.append(f"  Related bills: {', '.join(b['similar'])}")
            if b.get("verified_summary"):
                lines.append(f"  Summary: {b['verified_summary']}")
            lines.append(f"  Source: {b['url']}")
            lines.append("")

    committee = [b for b in status_changes if b["level"] == "committee"]
    if committee:
        lines.append("COMMITTEE / PROCEDURAL MOVEMENT")
        for b in committee:
            lines.append(f"[{b['jurisdiction']}] {b['identifier']} — {b['title']}")
            lines.append(f"  {b['previous_action']} -> {b['latest_action']} ({b['latest_date']})")
            lines.append(f"  Source: {b['url']}")
            lines.append("")

    minor_count = len([b for b in new_filings if b["level"] == "minor"]) + \
        len([b for b in status_changes if b["level"] == "minor"])
    if minor_count:
        lines.append(f"({minor_count} additional routine update(s) tracked — see daily digest for the full list.)")
        lines.append("")

    lines.append("—")
    lines.append("Automated legislative tracker (sources: Open States, Congress.gov). Verify bill text directly before external use.")
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENTS, msg.as_string())
    print("Alert email sent.")


def main():
    print("Scanning state legislative bills via Open States...")
    state_store = load_state()
    is_bootstrap_run = len(state_store) == 0

    new_filings, status_changes = scan_state_bills(state_store)

    print("Scanning federal bills via Congress.gov...")
    fed_new, fed_changes = scan_federal_bills(state_store)
    new_filings.extend(fed_new)
    status_changes.extend(fed_changes)

    save_state(state_store)
    print(f"New filings: {len(new_filings)} | Status changes: {len(status_changes)}")

    if is_bootstrap_run:
        print(f"First-ever run — baseline of {len(new_filings)} bills saved silently, no email sent.")
        return

    if not new_filings and not status_changes:
        print("No changes detected — no email sent.")
        return

    today = datetime.now().strftime("%B %-d, %Y %I:%M %p")
    body = build_alert_email(new_filings, status_changes)

    notable_new = [b for b in new_filings if b["level"] in ("significant", "committee")]
    notable_changes = [b for b in status_changes if b["level"] in ("significant", "committee")]

    if not notable_new and not notable_changes:
        print("Only routine/minor changes this run — no email sent.")
        return

    append_alerts_to_log(notable_new, notable_changes)

    subject_bits = []
    if any(b["level"] == "significant" for b in status_changes):
        subject_bits.append("SIGNIFICANT ACTION")
    if notable_new:
        subject_bits.append(f"{len(notable_new)} new filing(s)")
    tag = ", ".join(subject_bits) if subject_bits else "update"
    subject = f"EA Legislative Alert — {tag} — {today}"

    send_email(subject, body)


if __name__ == "__main__":
    main()
