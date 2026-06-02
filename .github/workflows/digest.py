import requests
import xml.etree.ElementTree as ET
import anthropic
import smtplib
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
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

RSS_FEEDS = [
    "https://electrek.co/feed/",
    "https://electrek.co/tag/ev-charging/feed/",
    "https://electrek.co/tag/electrify-america/feed/",
    "https://electrek.co/tag/nevi/feed/",
    "https://electrek.co/tag/electric-vehicle/feed/",
]

STATE_KEYWORDS = [
    "Massachusetts", "New Jersey", "New York", "Pennsylvania", "Maryland",
    "Virginia", "Utah", "Colorado", "Texas", "Washington", "Illinois", "Ohio",
    " MA ", " NJ ", " NY ", " PA ", " MD ", " VA ", " UT ", " CO ", " TX ", " WA ", " IL ", " OH ",
]

POLICY_KEYWORDS = [
    "nevi", "ev charging", "electrify america", "electric vehicle", "lcfs",
    "charging station", "fast charge", "dcfc", "level 2", "evse",
    "battery storage", "grid", "nec", "permit", "uptime", "registration fee",
]


# ── Helpers ────────────────────────────────────────────────────────────────────
def fetch_rss(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  WARNING: could not fetch {url}: {e}", file=sys.stderr)
        return None


def parse_recent_articles(xml_content: str, days: int = 7) -> list[dict]:
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        root = ET.fromstring(xml_content)
        for item in root.findall(".//item"):
            title        = item.findtext("title", "")
            link         = item.findtext("link", "")
            description  = item.findtext("description", "")
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
            relevant = (
                any(s.lower() in blob for s in STATE_KEYWORDS)
                or any(k in blob for k in POLICY_KEYWORDS)
            )
            if relevant:
                articles.append({
                    "title":       title,
                    "link":        link,
                    "description": description[:600],
                    "date":        pub_date_str,
                })
    except Exception as e:
        print(f"  WARNING: RSS parse error: {e}", file=sys.stderr)
    return articles


def compose_digest(articles: list[dict], today: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if articles:
        articles_text = "\n\n".join(
            f"Title: {a['title']}\nURL: {a['link']}\nDate: {a['date']}\nSummary: {a['description']}"
            for a in articles
        )
    else:
        articles_text = "No new articles found in RSS feeds today."

    prompt = f"""You are a policy monitoring assistant for ThinkJet, a government affairs consultancy.
Today is {today}. Based on the articles below from EV news RSS feeds, compose a daily policy digest email for the team monitoring Electrify America.

ARTICLES:
{articles_text}

STATES TO COVER (include every state, even with no news):
Massachusetts (MA), New Jersey (NJ), New York (NY), Pennsylvania (PA), Maryland (MD),
Virginia (VA), Utah (UT), Colorado (CO), Texas (TX), Washington (WA), Illinois (IL), Ohio (OH)

16 POLICY AREAS — tag each finding with the relevant area:
1. EV Charging Funding  2. LCFS  3. kWh Tax  4. Grid Reliability  5. Road Use Charges
6. NEVI ROI Caps  7. EVTIP  8. ADA Compliance  9. Data Sharing  10. Reliability & Uptime
11. Vandalism  12. Parking % Requirements  13. EV Registration Fees
14. Emergency Stop Button/NEC  — EA position: first responder access ONLY; flag with extra detail
15. Permit Expediting  16. Battery Storage/Fire Safety  — flag Texas items as ⚠️ HIGH PRIORITY

FORMAT (plain text, no markdown):

Team — here is your Electrify America policy and regulatory digest for {today}.

[For each state WITH findings:]
[STATE NAME] 🔴 ACTIVE
[Policy Area Tag] [1-2 sentence summary.]
Source: [URL]

[For each state with NO findings:]
[STATE NAME] — No significant developments today.

NATIONAL
[National/federal items, or "No significant national developments today."]

—
Prepared by ThinkJet monitoring tools for Electrify America.
Policy areas monitored: EV Charging Funding, LCFS, kWh Tax, Grid Reliability, Road Use Charges, NEVI ROI Caps, EVTIP, ADA, Data Sharing, Reliability/Uptime, Vandalism, Parking %, EV Reg Fees, Emergency Stop Button/NEC, Permit Expediting, Battery Storage/Fire Safety.
States monitored: MA, NJ, NY, PA, MD, VA, UT, CO, TX, WA, IL, OH.

Return ONLY the email body. Start with "Team —"."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


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


# ── Main ───────────────────────────────────────────────────────────────────────
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

    body    = compose_digest(unique, today)
    subject = f"Electrify America Policy & Regulatory Digest — {today}"

    send_email(subject, body)


if __name__ == "__main__":
    main()
