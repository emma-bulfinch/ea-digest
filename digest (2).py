import requests
import json
import xml.etree.ElementTree as ET
import smtplib
import re
import html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
import os
import sys

# ── Configuration ──────────────────────────────────────────────────────────────
SENDER = "emma@thinkjet.io"
RECIPIENTS = [
    "jefferson@thinkjet.io",
    "brianna@thinkjet.io",
    "ayah@thinkjet.io",
    "emma@thinkjet.io",
]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

RSS_FEEDS = [
    "https://electrek.co/feed/",
    "https://electrek.co/tag/ev-charging/feed/",
    "https://electrek.co/tag/electrify-america/feed/",
    "https://electrek.co/tag/nevi/feed/",
    "https://electrek.co/tag/electric-vehicle/feed/",
]

STATES = {
    "massachusetts": "MA",
    "new jersey":    "NJ",
    "new york":      "NY",
    "pennsylvania":  "PA",
    "maryland":      "MD",
    "virginia":      "VA",
    "utah":          "UT",
    "colorado":      "CO",
    "texas":         "TX",
    "washington":    "WA",
    "illinois":      "IL",
    "ohio":          "OH",
}

STATE_ORDER = [
    "Massachusetts", "New Jersey", "New York", "Pennsylvania",
    "Maryland", "Virginia", "Utah", "Colorado",
    "Texas", "Washington", "Illinois", "Ohio",
]

POLICY_AREAS = [
    (["nevi", "national electric vehicle infrastructure"],          "EV Charging Funding / NEVI"),
    (["ev charging fund", "charging grant", "charging investment",
      "charging infrastructure fund"],                              "EV Charging Funding"),
    (["low carbon fuel", "lcfs"],                                   "LCFS"),
    (["kwh tax", "kilowatt-hour tax", "charging tax", "sales tax"], "kWh Tax"),
    (["grid reliability", "interconnection", "demand charge",
      "time-of-use", "utility commission"],                         "Grid Reliability"),
    (["road use charge", "vmt fee", "vehicle miles traveled",
      "mileage fee"],                                               "Road Use Charges"),
    (["roi cap", "return on investment", "profit cap"],             "NEVI ROI Caps"),
    (["evtip", "ev infrastructure training", "technician certif"],  "EVTIP"),
    (["ada", "accessibility", "accessible"],                        "ADA Compliance"),
    (["data sharing", "usage data", "charging data"],               "Data Sharing"),
    (["uptime", "reliability report", "availability report"],       "Reliability & Uptime"),
    (["vandal", "theft", "cable cut"],                              "Vandalism"),
    (["parking requirement", "parking spaces", "parking code"],     "Parking % Requirements"),
    (["registration fee", "ev fee", "ev surcharge"],                "EV Registration Fees"),
    (["emergency stop", "nec code", "national electrical code"],    "Emergency Stop Button/NEC"),
    (["permit", "permitting", "building code", "zoning"],           "Permit Expediting"),
    (["battery storage", "fire safety", "fire code", "bess",
      "energy storage"],                                            "Battery Storage/Fire Safety"),
    (["electrify america"],                                         "EA Network"),
]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).strip()


def fetch_rss(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  WARNING: could not fetch {url}: {e}", file=sys.stderr)
        return None


def detect_states(blob: str) -> list[str]:
    found = []
    for name in STATES:
        if re.search(r'\b' + re.escape(name) + r'\b', blob):
            found.append(name.title())
    return found


def detect_policy_area(blob: str) -> str:
    for keywords, label in POLICY_AREAS:
        if any(kw in blob for kw in keywords):
            return label
    return "EV Charging"


def parse_recent_articles(xml_content: str, days: int = 7) -> list[dict]:
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        root = ET.fromstring(xml_content)
        for item in root.findall(".//item"):
            title        = strip_html(item.findtext("title", ""))
            link         = item.findtext("link", "").strip()
            description  = strip_html(item.findtext("description", ""))
            pub_date_str = item.findtext("pubDate", "")

            try:
                pub_date = parsedate_to_datetime(pub_date_str)
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
                if pub_date < cutoff:
                    continue
            except Exception:
                pass

            blob = f"{title} {description}".lower()

            states_found = detect_states(blob)
            is_national = any(kw in blob for kw in [
                "electrify america", "nevi", "federal", "fhwa", "doe ", "congress",
                "nationwide", "national", "h.r.", "senate", "house bill",
            ])

            if not states_found and not is_national:
                continue

            policy_area = detect_policy_area(blob)
            summary = description[:300].rstrip() + ("…" if len(description) > 300 else "")

            articles.append({
                "title":       title,
                "link":        link,
                "summary":     summary,
                "states":      states_found,
                "national":    is_national,
                "policy_area": policy_area,
                "date":        pub_date_str,
            })
    except Exception as e:
        print(f"  WARNING: RSS parse error: {e}", file=sys.stderr)
    return articles


def load_tracked_bills() -> dict:
    """Pulls the current legislative tracker snapshot (updated by bill_tracker.py)
    so the daily digest shows ongoing bill status, not just news mentions."""
    try:
        with open("data/bill_state.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def build_legislative_snapshot(bill_state: dict) -> list[str]:
    if not bill_state:
        return []
    by_state: dict[str, list[dict]] = {}
    for record in bill_state.values():
        by_state.setdefault(record["jurisdiction"], []).append(record)

    lines = ["", "ACTIVE LEGISLATION TRACKER (all bills currently being watched)"]
    for state in STATE_ORDER:
        bills = by_state.get(state, [])
        if not bills:
            continue
        lines.append(f"{state}:")
        for b in sorted(bills, key=lambda x: x.get("latest_date", ""), reverse=True):
            lines.append(f"  {b['identifier']} — {b['title'][:80]}")
            lines.append(f"    Status: {b['latest_action']} ({b['latest_date']})")
    lines.append("")
    return lines


def load_alert_log() -> list[dict]:
    try:
        with open("data/alert_log.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_alerts_section() -> list[str]:
    """
    Per Jefferson's request: fold legislative alerts into the daily digest,
    and explicitly say so even on quiet days, so it's clear the monitoring
    is active rather than just silent.
    """
    log = load_alert_log()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = [
        entry for entry in log
        if datetime.fromisoformat(entry["logged_at"]).replace(tzinfo=timezone.utc) > cutoff
    ]

    lines = ["LEGISLATIVE ALERTS — LAST 24 HOURS"]
    if not recent:
        lines.append("No significant legislative alerts in the last 24 hours. Monitoring is active.")
    else:
        for entry in recent:
            tag = "SIGNIFICANT" if entry["level"] == "significant" else "Committee/procedural"
            lines.append(f"[{tag}] [{entry['jurisdiction']}] {entry['identifier']} — {entry['title']}")
            lines.append(f"  {entry['detail']}")
            if entry.get("url"):
                lines.append(f"  Source: {entry['url']}")
    lines.append("")
    return lines


def build_email_body(articles: list[dict], today: str) -> str:
    by_state: dict[str, list[dict]] = {s: [] for s in STATE_ORDER}
    national: list[dict] = []

    for a in articles:
        placed = False
        for state in a["states"]:
            if state in by_state:
                by_state[state].append(a)
                placed = True
        if a["national"]:
            national.append(a)

    lines = [f"Team — here is your Electrify America policy and regulatory digest for {today}.", ""]
    lines.extend(build_alerts_section())
    lines.extend(build_legislative_snapshot(load_tracked_bills()))

    for state in STATE_ORDER:
        items = by_state[state]
        if items:
            lines.append(f"{state.upper()} 🔴 ACTIVE")
            for a in items:
                note = ""
                if a["policy_area"] == "Emergency Stop Button/NEC":
                    note = " ⚠️ NOTE: EA's position is that emergency stop access should be for FIRST RESPONDERS ONLY."
                if a["policy_area"] == "Battery Storage/Fire Safety" and state == "Texas":
                    note = " ⚠️ HIGH PRIORITY"
                lines.append(f"[{a['policy_area']}]{note} {a['title']}")
                lines.append(f"  {a['summary']}")
                lines.append(f"  Source: {a['link']}")
        else:
            lines.append(f"{state.upper()} — No significant developments today.")
        lines.append("")

    lines.append("NATIONAL")
    if national:
        for a in national:
            lines.append(f"[{a['policy_area']}] {a['title']}")
            lines.append(f"  {a['summary']}")
            lines.append(f"  Source: {a['link']}")
    else:
        lines.append("No significant national developments today.")
    lines.append("")
    lines.append("—")
    lines.append("Prepared by ThinkJet monitoring tools for Electrify America.")
    lines.append(
        "Policy areas monitored: EV Charging Funding, LCFS, kWh Tax, "
        "Grid Reliability, Road Use Charges, NEVI ROI Caps, EVTIP, ADA, "
        "Data Sharing, Reliability/Uptime, Vandalism, Parking %, EV Reg Fees, "
        "Emergency Stop Button/NEC, Permit Expediting, Battery Storage/Fire Safety."
    )
    lines.append("States monitored: MA, NJ, NY, PA, MD, VA, UT, CO, TX, WA, IL, OH.")

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"]    = SENDER
    msg["To"]      = ", ".join(RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENTS, msg.as_string())
    print("✓ Email sent successfully.")


def main():
    today = datetime.now().strftime("%B %-d, %Y")
    print(f"Running digest for {today}...")

    all_articles: list[dict] = []
    for feed in RSS_FEEDS:
        xml = fetch_rss(feed)
        if xml:
            found = parse_recent_articles(xml)
            print(f"  {feed}: {len(found)} relevant articles")
            all_articles.extend(found)

    seen: set[str] = set()
    unique = [a for a in all_articles if not (a["link"] in seen or seen.add(a["link"]))]
    print(f"Total unique articles: {len(unique)}")

    body    = build_email_body(unique, today)
    subject = f"Electrify America Policy & Regulatory Digest — {today}"

    send_email(subject, body)


if __name__ == "__main__":
    main()
