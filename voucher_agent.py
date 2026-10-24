import os
import json
import hashlib
import requests
import feedparser
from pathlib import Path
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

STATE_FILE = Path("state.json")
REPORT_FILE = Path("daily_report.md")

QUERIES = [
    '"Microsoft certification" voucher',
    '"PL-300" exam discount OR voucher',
    '"Microsoft Learn" free certification exam',
    '"Microsoft Virtual Training Days" certification voucher',
    '"Power Platform" certification exam discount',
    '"Dynamics 365" certification voucher',
    'Microsoft certification exam discount India',
    'Microsoft partner certification voucher',
]

OFFICIAL_DOMAINS = (
    "microsoft.com",
    "learn.microsoft.com",
    "events.microsoft.com",
    "esi.microsoft.com",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MicrosoftVoucherMonitor/1.0)"
}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def search_news(query):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        return feed.entries
    except Exception as exc:
        print(f"Search failed for {query!r}: {exc}")
        return []


def clean_text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def analyze_entry(entry):
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", ""))
    link = entry.get("link", "")
    published = entry.get("published", "Date unavailable")

    text = f"{title} {summary}".lower()

    positive_terms = (
        "voucher", "discount", "free exam", "exam offer",
        "certification offer", "exam rebate", "exam savings",
        "reduced price", "free certification",
    )

    negative_terms = (
        "expired voucher", "voucher scam", "exam dumps",
    )

    if not any(term in text for term in positive_terms):
        return None

    if any(term in text for term in negative_terms):
        return None

    official = any(domain in link.lower() for domain in OFFICIAL_DOMAINS)

    certification = "Microsoft certification (verify exam)"
    if "pl-300" in text or "power bi" in text:
        certification = "PL-300 / Power BI"
    elif "power platform" in text or "power automate" in text:
        certification = "Power Platform"
    elif "dynamics 365" in text:
        certification = "Dynamics 365"
    elif "azure" in text:
        certification = "Azure"
    elif "student" in text or "academic" in text:
        certification = "Student / academic offer"

    return {
        "title": title,
        "summary": summary[:700],
        "url": link,
        "published": published,
        "official_source": official,
        "certification": certification,
        "hash": hashlib.sha256(
            (title + link).encode("utf-8")
        ).hexdigest(),
    }


def main():
    state = load_state()
    seen = set(state.get("seen", []))
    results = {}
    successful_searches = 0

    for query in QUERIES:
        entries = search_news(query)

        if entries:
            successful_searches += 1

        for entry in entries:
            item = analyze_entry(entry)
            if item:
                results[item["hash"]] = item

    new_items = [
        item for key, item in results.items()
        if key not in seen
    ]

    # Keep the history bounded.
    all_seen = list(seen.union(results.keys()))
    state["seen"] = all_seen[-5000:]
    save_state(state)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Microsoft Certification Voucher Monitor",
        "",
        f"Checked: {now}",
        f"Searches with results: {successful_searches}/{len(QUERIES)}",
        f"Potential opportunities found: {len(results)}",
        f"New opportunities: {len(new_items)}",
        "",
        "> Search results are leads, not confirmed discounts. "
        "Verify eligibility, expiry, exam applicability and India "
        "availability on the original source before acting.",
        "",
    ]

    if not successful_searches:
        lines.extend([
            "Search feeds returned no usable results. This does not "
            "mean no offers exist. Check the workflow logs.",
            "",
        ])

    if new_items:
        for item in new_items:
            source_type = (
                "Official-domain link"
                if item["official_source"]
                else "Third-party result — verify independently"
            )
            lines.extend([
                f"## {item['title']}",
                "",
                f"- **Likely category:** {item['certification']}",
                f"- **Published:** {item['published']}",
                f"- **Source status:** {source_type}",
                f"- **Link:** {item['url']}",
                "",
                item["summary"],
                "",
            ])
    else:
        lines.extend([
            "No new potential opportunities detected today.",
            "",
            "Previously discovered items are not repeated. "
            "The absence of new results is not proof that no "
            "voucher or discount is available.",
            "",
        ])

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if token and chat_id and new_items:
        message = (
            "🎓 Microsoft Certification Voucher Monitor\n\n"
            + "\n\n".join(
                f"{item['title']}\n{item['url']}"
                for item in new_items[:8]
            )
            + "\n\nVerify eligibility and validity before relying "
              "on any offer."
        )

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message[:4000],
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            response.raise_for_status()
            print("Telegram notification sent.")
        except Exception as exc:
            print(f"Telegram notification failed: {exc}")


if __name__ == "__main__":
    main()
