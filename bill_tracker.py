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
    if not notable_new and not any(b["level"] in ("significant", "committee") for b in status_changes):
        print("Only routine/minor changes this run — no email sent.")
        return

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
