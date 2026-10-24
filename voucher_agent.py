import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import requests
from bs4 import BeautifulSoup

SOURCES_FILE = Path("sources.json")
REGISTRY_FILE = Path("source_registry.json")
STATE_FILE = Path("state.json")
REPORT_FILE = Path("daily_report.md")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MicrosoftVoucherMonitor/1.1; +https://github.com/joshua04d/ms-cert-voucher-agent)"
}
TIMEOUT_SECONDS = 20

OFFICIAL_HOSTS = (
    "microsoft.com",
    "learn.microsoft.com",
    "events.microsoft.com",
    "esi.microsoft.com",
    "trainingsupport.microsoft.com",
)
COMMUNITY_HOSTS = (
    "techcommunity.microsoft.com",
    "community.powerplatform.com",
    "community.fabric.microsoft.com",
    "reddit.com",
)
TRAINING_HOST_HINTS = ("coursera.org", "datacamp.com", "pluralsight.com", "udemy.com")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def normalized_host(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def host_matches(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def classify_host(host):
    if host_matches(host, OFFICIAL_HOSTS):
        return "official"
    if host_matches(host, COMMUNITY_HOSTS):
        return "community"
    if host_matches(host, TRAINING_HOST_HINTS):
        return "training_provider_candidate"
    if not host:
        return "unknown"
    return "unverified_candidate"


def load_config():
    config = load_json(SOURCES_FILE, {"settings": {}, "sources": []})
    if not config.get("sources"):
        raise RuntimeError("sources.json is missing or has no configured sources.")
    return config


def load_state():
    return load_json(STATE_FILE, {"seen": []})

def parse_published(entry):
    """Extract a publication/update date from a feed entry."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)

        if value:
            try:
                import calendar
                return datetime.fromtimestamp(
                    calendar.timegm(value),
                    tz=timezone.utc
                )
            except (TypeError, ValueError, OverflowError):
                pass

    for key in ("published", "updated", "created", "date"):
        value = entry.get(key)

        if not value:
            continue

        try:
            parsed = parsedate_to_datetime(value)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

    return None


def search_google_news(query, max_results=30):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    return feed.entries[:max_results]


def search_reddit(source, max_results=30):
    subreddit = source["subreddit"]
    query = quote_plus(source["query"])
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.rss"
        f"?q={query}&restrict_sr=1&sort=new&t=month"
    )
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    return feed.entries[:max_results]


def collect_source(source, max_results=30):
    source_type = source.get("type")
    if source_type == "google_news_query":
        return search_google_news(source["query"], max_results)
    if source_type == "reddit_search":
        return search_reddit(source, max_results)
    if source_type == "rss":
        response = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return feedparser.parse(response.content).entries[:max_results]
    raise ValueError(f"Unsupported source type: {source_type}")


def analyze_entry(entry, source):
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", entry.get("description", "")))
    link = entry.get("link", "")
    published = entry.get("published", entry.get("updated", "Date unavailable"))
    publisher = entry.get("source", {}) or {}
    publisher_url = publisher.get("href", "") if isinstance(publisher, dict) else ""
    publisher_name = publisher.get("title", "") if isinstance(publisher, dict) else ""

    combined = f"{title} {summary}".lower()
    positive_terms = (
        "voucher", "discount", "free exam", "exam offer", "certification offer",
        "exam rebate", "exam savings", "reduced price", "free certification",
        "promo code", "promotion", "giveaway", "challenge", "exam discount",
    )
    negative_terms = ("exam dumps", "voucher scam", "braindump")
    if not any(term in combined for term in positive_terms):
        return None
    if any(term in combined for term in negative_terms):
        return None
    if not link:
        return None

    certification = "Microsoft certification (verify exact exam)"
    if "pl-300" in combined or "power bi" in combined:
        certification = "PL-300 / Power BI"
    elif "power platform" in combined or "power automate" in combined:
        certification = "Power Platform"
    elif "dynamics 365" in combined:
        certification = "Dynamics 365"
    elif "azure" in combined:
        certification = "Azure"
    elif "fabric" in combined:
        certification = "Microsoft Fabric"

    item_hash = hashlib.sha256((title + link).encode("utf-8")).hexdigest()
    publisher_host = normalized_host(publisher_url)
    article_host = normalized_host(link)
    # Prefer the publisher's canonical domain when a Google News RSS item supplies it.
    candidate_url = publisher_url or link
    candidate_host = normalized_host(candidate_url)

    return {
        "title": title,
        "summary": summary[:900],
        "url": link,
        "publisher_url": publisher_url,
        "publisher": publisher_name or source.get("name", "Unknown source"),
        "publisher_host": publisher_host or article_host,
        "published": published,
        "discovered_at": utc_now(),
        "source_id": source.get("id", "unknown"),
        "source_name": source.get("name", "Unknown source"),
        "source_category": source.get("category", "unknown"),
        "source_trust_tier": source.get("trust_tier", "unverified"),
        "candidate_source_url": candidate_url,
        "candidate_source_host": candidate_host,
        "candidate_source_class": classify_host(candidate_host),
        "official_source": host_matches(candidate_host, OFFICIAL_HOSTS),
        "certification": certification,
        "hash": item_hash,
    }


def seed_registry(registry, sources):
    registry.setdefault("sources", {})
    for source in sources:
        source_id = source["id"]
        current = registry["sources"].get(source_id, {})
        current.update({
            "id": source_id,
            "name": source.get("name", source_id),
            "configured_type": source.get("type", "unknown"),
            "category": source.get("category", "unknown"),
            "trust_tier": source.get("trust_tier", "unverified"),
            "query": source.get("query"),
            "subreddit": source.get("subreddit"),
            "url": source.get("url"),
            "status": current.get("status", "active"),
            "first_seen_at": current.get("first_seen_at", utc_now()),
            "last_checked_at": current.get("last_checked_at"),
            "checks": current.get("checks", 0),
            "errors": current.get("errors", 0),
            "entries_seen": current.get("entries_seen", 0),
            "relevant_results": current.get("relevant_results", 0),
            "examples": current.get("examples", []),
        })
        registry["sources"][source_id] = current
    return registry


def update_registry_for_source(registry, source, entries, relevant_items, error=None):
    record = registry["sources"].setdefault(source["id"], {
        "id": source["id"], "name": source.get("name", source["id"]),
        "status": "active", "checks": 0, "errors": 0, "entries_seen": 0,
        "relevant_results": 0, "examples": [], "first_seen_at": utc_now(),
    })
    record["last_checked_at"] = utc_now()
    record["checks"] = record.get("checks", 0) + 1
    record["entries_seen"] = record.get("entries_seen", 0) + len(entries)
    record["relevant_results"] = record.get("relevant_results", 0) + len(relevant_items)
    if error:
        record["errors"] = record.get("errors", 0) + 1
        record["last_error"] = str(error)[:300]
    else:
        record.pop("last_error", None)
    examples = record.setdefault("examples", [])
    for item in relevant_items:
        if item["url"] not in examples:
            examples.append(item["url"])
    record["examples"] = examples[-5:]


def register_candidate_domains(registry, items, max_candidates=500):
    """Store newly encountered publisher domains as candidates, never auto-trust them."""
    records = registry.setdefault("sources", {})
    candidate_by_host = {
        item.get("candidate_source_host"): item
        for item in items
        if item.get("candidate_source_host")
    }
    for host, item in candidate_by_host.items():
        if not host or host in ("news.google.com", "www.reddit.com"):
            continue
        existing = next(
            (r for r in records.values() if r.get("host") == host),
            None,
        )
        if existing:
            existing["discovery_count"] = existing.get("discovery_count", 0) + 1
            existing["last_discovered_at"] = utc_now()
            examples = existing.setdefault("examples", [])
            if item["url"] not in examples:
                examples.append(item["url"])
            existing["examples"] = examples[-5:]
            continue

        candidate_id = "candidate_" + hashlib.sha1(host.encode("utf-8")).hexdigest()[:12]
        if candidate_id in records:
            continue
        if len(records) >= max_candidates:
            break
        records[candidate_id] = {
            "id": candidate_id,
            "name": host,
            "host": host,
            "url": item.get("candidate_source_url") or item["url"],
            "category": "discovered_candidate",
            "trust_tier": item.get("candidate_source_class", "unverified_candidate"),
            "status": "candidate",
            "first_seen_at": utc_now(),
            "last_discovered_at": utc_now(),
            "discovery_count": 1,
            "discovered_via": item.get("source_id"),
            "examples": [item["url"]],
            "note": "Automatically discovered; verify before treating as trusted or activating as a collector.",
        }


def main():
    config = load_config()
    settings = config.get("settings", {})
    max_results = int(settings.get("max_results_per_source", 30))
    max_candidates = int(settings.get("max_candidate_sources", 500))

    registry = load_json(REGISTRY_FILE, {"version": 1, "sources": {}})
    registry = seed_registry(registry, config["sources"])
    state = load_state()
    seen = set(state.get("seen", []))

    results = {}
    successful_sources = 0
    errors = []
    source_summaries = []

    for source in config["sources"]:
        if source.get("enabled", True) is False:
            continue
        entries = []
        relevant_items = []
        error = None
        try:
            entries = collect_source(source, max_results=max_results)
            successful_sources += 1
            for entry in entries:
                item = analyze_entry(entry, source)
                if item:
                    results[item["hash"]] = item
                    relevant_items.append(item)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append((source.get("name", source["id"]), error))
            print(f"[WARN] {source.get('name', source['id'])}: {error}")

        update_registry_for_source(registry, source, entries, relevant_items, error)
        source_summaries.append({
            "name": source.get("name", source["id"]),
            "entries": len(entries),
            "relevant": len(relevant_items),
            "error": bool(error),
        })

    new_items = [item for key, item in results.items() if key not in seen]
    register_candidate_domains(registry, list(results.values()), max_candidates=max_candidates)

    # Keep deduplication history bounded.
    state["seen"] = list(seen.union(results.keys()))[-5000:]
    state["last_run_at"] = utc_now()
    save_json(STATE_FILE, state)

    registry["updated_at"] = utc_now()
    registry["source_count"] = len(registry.get("sources", {}))
    save_json(REGISTRY_FILE, registry)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Microsoft Certification Voucher Monitor",
        "",
        f"- Checked: {now}",
        f"- Configured sources: {len(config['sources'])}",
        f"- Sources checked successfully: {successful_sources}",
        f"- Sources with errors: {len(errors)}",
        f"- Source registry entries: {registry['source_count']}",
        f"- Potential opportunities found: {len(results)}",
        f"- New opportunities: {len(new_items)}",
        "",
        "> Search results are leads, not confirmed discounts. Verify current validity, "
        "eligibility, exam applicability, dates, and India availability on the original source.",
        "",
        "## New potential opportunities",
        "",
    ]

    if new_items:
        for item in new_items[:40]:
            status = "Official-domain publisher (still verify offer)" if item["official_source"] else "Community/third-party lead — independently verify"
            lines.extend([
                f"### {item['title']}",
                "",
                f"- **Likely certification:** {item['certification']}",
                f"- **Published:** {item['published']}",
                f"- **Discovered:** {item['discovered_at']}",
                f"- **Publisher:** {item['publisher']}",
                f"- **Publisher/source status:** {status}",
                f"- **Found through:** {item['source_name']}",
                f"- **Source URL:** {item.get('publisher_url') or 'Not supplied'}",
                f"- **Article URL:** {item['url']}",
                "",
                item["summary"],
                "",
            ])
    else:
        lines.append("No new potential opportunities detected in this run. This does not prove that no offers exist.")
        lines.append("")

    lines.extend(["## Source health", ""])
    for summary in source_summaries:
        marker = "⚠️" if summary["error"] else "✓"
        lines.append(
            f"- {marker} **{summary['name']}** — {summary['entries']} entries, "
            f"{summary['relevant']} potential leads"
        )
    if errors:
        lines.extend(["", "## Source errors", ""])
        for name, error in errors:
            lines.append(f"- **{name}:** `{error}`")

    lines.extend([
        "",
        "## Registry notes",
        "",
        "- Newly encountered domains are stored as candidates, not automatically trusted.",
        "- This version records source performance and discoveries; it does not yet crawl arbitrary links.",
        "- Publication dates come from feeds and may be missing or inaccurate.",
        "",
    ])

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id and new_items:
        chunks = []
        for item in new_items[:8]:
            chunks.append(
                f"🎓 {item['title']}\n"
                f"Category: {item['certification']}\n"
                f"Published: {item['published']}\n"
                f"Found via: {item['source_name']}\n"
                f"{item['url']}"
            )
        message = "Microsoft Certification Voucher Monitor\n\n" + "\n\n".join(chunks)
        message += "\n\nVerify eligibility and expiry on the original source."
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            print("Telegram notification sent.")
        except Exception as exc:
            print(f"Telegram notification failed: {exc}")


if __name__ == "__main__":
    main()
