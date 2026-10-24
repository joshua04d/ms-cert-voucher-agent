
import os
import json
import hashlib
import requests
import feedparser

from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
from bs4 import BeautifulSoup


STATE_FILE = Path("state.json")
REPORT_FILE = Path("daily_report.md")
CONFIG_FILE = Path("sources.json")

# Default searches. Keep these in sync with your target certifications.
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

DEFAULT_MAX_AGE_DAYS = 183
DEFAULT_INCLUDE_UNDATED = False


def load_settings():
    """Read optional recency settings from sources.json."""
    settings = {}

    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                settings = config.get("settings", {})
                if not isinstance(settings, dict):
                    settings = {}
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not read settings from {CONFIG_FILE}: {exc}")

    # Environment variable overrides the JSON setting.
    try:
        max_age_days = int(
            os.getenv(
                "MAX_AGE_DAYS",
                settings.get("max_age_days", DEFAULT_MAX_AGE_DAYS),
            )
        )
    except (TypeError, ValueError):
        max_age_days = DEFAULT_MAX_AGE_DAYS

    # Prevent invalid or accidental zero/negative date windows.
    max_age_days = max(1, min(max_age_days, 365))

    include_undated_value = os.getenv(
        "INCLUDE_UNDATED",
        str(settings.get("include_undated", DEFAULT_INCLUDE_UNDATED)),
    )
    include_undated = str(include_undated_value).strip().lower() in {
        "1", "true", "yes", "y"
    }

    return max_age_days, include_undated


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state.setdefault("seen", [])
                return state
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not load state file: {exc}")

    return {"seen": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def search_news(query, max_age_days):
    """Search Google News RSS, with an additional date restriction."""
    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
    ).date().isoformat()

    query_with_date = f"{query} after:{cutoff_date}"

    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query_with_date)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )

    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()

        feed = feedparser.parse(response.content)

        if getattr(feed, "bozo", False):
            print(f"Feed parsing warning for {query!r}: {feed.bozo_exception}")

        return feed.entries

    except Exception as exc:
        print(f"Search failed for {query!r}: {exc}")
        return []


def clean_text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(
        " ", strip=True
    )


def parse_published(entry):
    """
    Return the article publication date as a timezone-aware datetime.

    Supports common RSS fields, RFC email dates, and ISO date strings.
    Returns None when no reliable publication date is available.
    """
    # feedparser normally provides these parsed fields for RSS entries.
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)

        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    # Fallback to text date fields.
    for field in ("published", "updated", "created", "date"):
        value = entry.get(field)

        if not value:
            continue

        value = str(value).strip()

        # Try RFC 2822-style dates, often used in RSS.
        try:
            parsed_date = parsedate_to_datetime(value)

            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)

            return parsed_date.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

        # Try ISO 8601 dates.
        try:
            normalized = value.replace("Z", "+00:00")
            parsed_date = datetime.fromisoformat(normalized)

            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)

            return parsed_date.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

    return None


def is_official_url(url):
    """Check the actual hostname instead of a substring in the URL."""
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
        return any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in OFFICIAL_DOMAINS
        )
    except (TypeError, ValueError):
        return False


def analyze_entry(
    entry,
    max_age_days=DEFAULT_MAX_AGE_DAYS,
    include_undated=DEFAULT_INCLUDE_UNDATED,
):
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", ""))
    link = entry.get("link", "")

    if not title or not link:
        return None

    published_dt = parse_published(entry)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)

    # Exclude undated results unless explicitly enabled.
    if published_dt is None and not include_undated:
        return None

    # Enforce the publication window in Python.
    # This is the final guard; the search engine's date filter alone
    # is not guaranteed to be exact.
    if published_dt is not None:
        if published_dt < cutoff:
            return None

        # Allow a small tolerance for clock or feed discrepancies.
        if published_dt > now + timedelta(days=1):
            return None

    published_display = (
        published_dt.strftime("%Y-%m-%d %H:%M UTC")
        if published_dt
        else "Date unavailable"
    )

    text = f"{title} {summary}".lower()

    positive_terms = (
        "voucher",
        "discount",
        "free exam",
        "exam offer",
        "certification offer",
        "exam rebate",
        "exam savings",
        "reduced price",
        "free certification",
        "exam discount",
        "exam promotion",
        "certification discount",
        "50% off",
        "50% discount",
    )

    negative_terms = (
        "voucher scam",
        "exam dumps",
    )

    if not any(term in text for term in positive_terms):
        return None

    if any(term in text for term in negative_terms):
        return None

    official = is_official_url(link)

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
        "published": published_display,
        "published_iso": published_dt.isoformat() if published_dt else None,
        "official_source": official,
        "certification": certification,
        "hash": hashlib.sha256(
            (title + link).encode("utf-8")
        ).hexdigest(),
    }


def build_report(results, new_items, successful_searches, max_age_days,
                 include_undated):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Microsoft Certification Voucher Monitor",
        "",
        f"Checked: {now}",
        f"Searches with results: {successful_searches}/{len(QUERIES)}",
        f"Publication window: last {max_age_days} days",
        f"Undated results included: {include_undated}",
        f"Potential opportunities found: {len(results)}",
        f"New opportunities: {len(new_items)}",
        "",
        "> Search results are leads, not confirmed discounts. Verify "
        "eligibility, voucher expiry, exam applicability, and India "
        "availability on the original source before acting.",
        "",
    ]

    if not successful_searches:
        lines.extend([
            "Search feeds returned no usable results. This does not mean "
            "no offers exist. Check the GitHub Actions workflow logs.",
            "",
        ])

    if new_items:
        for item in new_items:
            source_status = (
                "Official Microsoft domain — verify offer details"
                if item["official_source"]
                else "Third-party result — verify independently"
            )

            lines.extend([
                f"## {item['title']}",
                "",
                f"- **Likely category:** {item['certification']}",
                f"- **Published:** {item['published']}",
                f"- **Source status:** {source_status}",
                f"- **Link:** {item['url']}",
                "",
                item["summary"],
                "",
            ])
    else:
        lines.extend([
            "No new potential opportunities detected today.",
            "",
            "Previously discovered items are not repeated. No new results "
            "does not prove that no voucher or discount is available.",
            "",
        ])

    return "\n".join(lines)


def send_telegram_notification(new_items):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram notification skipped: secrets are not configured.")
        return

    if not new_items:
        print("Telegram notification skipped: no new opportunities.")
        return

    message_parts = [
        "🎓 Microsoft Certification Voucher Monitor",
        "",
    ]

    for item in new_items[:8]:
        message_parts.append(
            f"{item['title']}\n"
            f"Category: {item['certification']}\n"
            f"Published: {item['published']}\n"
            f"{item['url']}"
        )

    message_parts.extend([
        "",
        "Verify eligibility and voucher validity before relying on any offer.",
    ])

    message = "\n\n".join(message_parts)

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


def main():
    max_age_days, include_undated = load_settings()

    print(f"Using publication window: {max_age_days} days")
    print(f"Include undated results: {include_undated}")

    state = load_state()
    seen = set(state.get("seen", []))

    results = {}
    successful_searches = 0

    for query in QUERIES:
        print(f"Searching: {query}")

        entries = search_news(query, max_age_days)

        if entries:
            successful_searches += 1

        for entry in entries:
            item = analyze_entry(
                entry,
                max_age_days=max_age_days,
                include_undated=include_undated,
            )

            if item:
                results[item["hash"]] = item

    new_items = [
        item
        for key, item in results.items()
        if key not in seen
    ]

    # Preserve deduplication history while keeping the file bounded.
    all_seen = list(seen.union(results.keys()))
    state["seen"] = all_seen[-5000:]
    save_state(state)

    report = build_report(
        results=results,
        new_items=new_items,
        successful_searches=successful_searches,
        max_age_days=max_age_days,
        include_undated=include_undated,
    )

    REPORT_FILE.write_text(report, encoding="utf-8")
    print(report)

    send_telegram_notification(new_items)


if __name__ == "__main__":
    main()
