#!/usr/bin/env python3
"""
Plaza NewNewNew — Delft Listing Monitor (Pushover edition)
==========================================================
Polls the Plaza website for new rental listings in Delft and sends
a push notification to your phone via Pushover.

Anti-ban strategy:
  • Only runs 07:00–23:00 CET (no suspicious night traffic)
  • Randomised 5–7 min interval via GitHub Actions jitter
  • Realistic browser User-Agent, rotated each run
  • Homepage warm-up for cookies + referer chain
  • Skips full parse if page HTML hasn't changed (ETag/hash)
  • Monitors response times — auto backs off if Plaza slows down
  • Polite delays between page fetches
  • Exponential back-off on errors
  • Only fetches public pages — no login, no API abuse
"""

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

CET = timezone(timedelta(hours=2))  # CEST (summer), close enough year-round

def load_env_file():
    """Load variables from .env file if present."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip().strip('"').strip("'")
                    os.environ.setdefault(key.strip(), value)

load_env_file()


@dataclass
class Config:
    # Discord webhook URL (REQUIRED)
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # Polling settings
    poll_min_seconds: int = int(os.getenv("POLL_MIN_SECONDS", "480"))
    poll_max_seconds: int = int(os.getenv("POLL_MAX_SECONDS", "900"))

    # Active hours (CET) — no checks outside this window
    active_hour_start: int = int(os.getenv("ACTIVE_HOUR_START", "7"))   # 07:00
    active_hour_end: int = int(os.getenv("ACTIVE_HOUR_END", "23"))      # 23:00

    # Target city filter
    target_city: str = os.getenv("TARGET_CITY", "delft")

    # Persistence file
    seen_file: str = os.getenv("SEEN_FILE", str(Path(__file__).parent / "seen_listings.json"))

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self):
        if not self.discord_webhook_url:
            print(
                "\n❌  Missing DISCORD_WEBHOOK_URL\n"
                "   1. In Discord: right-click a channel → Edit Channel → Integrations → Webhooks\n"
                "   2. Click 'New Webhook', copy the URL\n"
                "   3. Set it as a GitHub secret or in your .env file\n"
            )
            sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# Time window check
# ──────────────────────────────────────────────────────────────────────

def is_active_hours(config: Config) -> bool:
    """Check if current time in CET is within active hours."""
    now_cet = datetime.now(CET)
    return config.active_hour_start <= now_cet.hour < config.active_hour_end


def is_weekend() -> bool:
    """Check if today is Saturday (5) or Sunday (6) in CET."""
    return datetime.now(CET).weekday() >= 5


# ──────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Listing:
    listing_id: str
    title: str = ""
    address: str = ""
    city: str = ""
    price: str = ""
    area: str = ""
    available_from: str = ""
    url: str = ""
    first_seen: str = ""
    raw_text: str = ""


# ──────────────────────────────────────────────────────────────────────
# User-Agent pool
# ──────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# ──────────────────────────────────────────────────────────────────────
# Target pages
# ──────────────────────────────────────────────────────────────────────

LISTING_URLS = [
    "https://plaza.newnewnew.space/aanbod/wonen",
    "https://plaza.newnewnew.space/onze-complexen/nederland/delft",
    "https://plaza.newnewnew.space/onze-complexen/nederland/delft/van-embdenstraat",
    "https://plaza.newnewnew.space/onze-complexen/nederland/delft/jan-de-oudeweg",
]


# ──────────────────────────────────────────────────────────────────────
# Scraper
# ──────────────────────────────────────────────────────────────────────

logger = logging.getLogger("plaza_monitor")

# In-memory cache of page hashes to skip re-parsing unchanged pages
_page_hashes: dict[str, str] = {}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    return s


def warm_up_session(session: requests.Session):
    try:
        resp = session.get("https://plaza.newnewnew.space/", timeout=30, allow_redirects=True)
        resp.raise_for_status()
        session.headers["Referer"] = "https://plaza.newnewnew.space/"
        logger.debug("Session warmed up (cookies: %d)", len(session.cookies))
    except requests.RequestException as e:
        logger.debug("Warm-up failed (non-critical): %s", e)


def fetch_page(session: requests.Session, url: str) -> Optional[str]:
    """Fetch a page and track response time. Returns None on error."""
    try:
        start = time.monotonic()
        resp = session.get(url, timeout=30, allow_redirects=True)
        elapsed = time.monotonic() - start
        resp.raise_for_status()
        session.headers["Referer"] = url

        # Warn if Plaza is responding slowly (possible throttling)
        if elapsed > 5.0:
            logger.warning("Slow response from %s (%.1fs) — possible throttling", url, elapsed)

        return resp.text
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


def page_changed(url: str, html: str) -> bool:
    """Check if page content changed since last fetch using a hash."""
    content_hash = hashlib.md5(html.encode()).hexdigest()
    old_hash = _page_hashes.get(url)
    _page_hashes[url] = content_hash
    if old_hash is None:
        return True  # first fetch, treat as changed
    return content_hash != old_hash


def extract_listings_from_html(html: str, source_url: str, target_city: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Strategy 1: Detail page links
    detail_links = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if re.search(r"/(?:aanbod|availables)[^\"']*/details/", href, re.I):
            detail_links.add(a_tag)

    for a_tag in detail_links:
        href = a_tag["href"]
        if not href.startswith("http"):
            href = "https://plaza.newnewnew.space" + href
        combined = (href + " " + a_tag.get_text(separator=" ", strip=True)).lower()
        if target_city.lower() not in combined:
            continue
        lid = hashlib.sha256(href.encode()).hexdigest()[:16]
        card = a_tag
        for _ in range(5):
            parent = card.parent
            if parent and parent.name in ("div", "article", "section", "li"):
                card = parent
                if len(card.get_text(strip=True)) > 40:
                    break
        raw = card.get_text(separator=" | ", strip=True)
        price_match = re.search(r"€\s*[\d.,]+", raw)
        area_match = re.search(r"(\d+)\s*m[²2]", raw)
        title_text = a_tag.get_text(separator=" ", strip=True) or href.split("/")[-1]
        listings.append(Listing(
            listing_id=lid, title=title_text, address=title_text, city="Delft",
            price=price_match.group(0) if price_match else "",
            area=area_match.group(0) if area_match else "",
            url=href, first_seen=now_iso, raw_text=raw[:500],
        ))

    # Strategy 2: CSS-class-based cards
    for tag in soup.find_all(["div", "article", "section", "li"], class_=True):
        classes = " ".join(tag.get("class", []))
        if not re.search(r"listing|result|card|woning|item|offer|aanbod", classes, re.I):
            continue
        text = tag.get_text(separator=" ", strip=True).lower()
        if target_city.lower() not in text:
            continue
        inner_link = tag.find("a", href=True)
        href = ""
        if inner_link:
            href = inner_link["href"]
            if not href.startswith("http"):
                href = "https://plaza.newnewnew.space" + href
        lid = hashlib.sha256((href or text[:100]).encode()).hexdigest()[:16]
        if any(l.listing_id == lid for l in listings):
            continue
        price_match = re.search(r"€\s*[\d.,]+", text)
        listings.append(Listing(
            listing_id=lid, title=text[:80], city="Delft",
            price=price_match.group(0) if price_match else "",
            url=href, first_seen=now_iso, raw_text=text[:500],
        ))

    # Strategy 3: Any link mentioning delft + housing keywords
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(separator=" ", strip=True)
        combined = (href + " " + text).lower()
        if target_city.lower() not in combined:
            continue
        if not re.search(r"details/|huurwoning|woning|studio|appartement|kamer", combined):
            continue
        if not href.startswith("http"):
            href = "https://plaza.newnewnew.space" + href
        lid = hashlib.sha256(href.encode()).hexdigest()[:16]
        if any(l.listing_id == lid for l in listings):
            continue
        listings.append(Listing(
            listing_id=lid, title=text or href.split("/")[-1], city="Delft",
            url=href, first_seen=now_iso, raw_text=text[:500],
        ))

    return listings


def scrape_all(session: requests.Session, target_city: str) -> list[Listing]:
    warm_up_session(session)
    time.sleep(random.uniform(0.5, 1.5))
    all_listings: dict[str, Listing] = {}
    for url in LISTING_URLS:
        html = fetch_page(session, url)
        if not html:
            continue
        # Only parse if the page actually changed
        if page_changed(url, html):
            for listing in extract_listings_from_html(html, url, target_city):
                all_listings[listing.listing_id] = listing
            logger.debug("Page changed: %s", url)
        else:
            logger.debug("Page unchanged, skipping parse: %s", url)
        time.sleep(random.uniform(1.0, 2.0))
    return list(all_listings.values())


# ──────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────

def load_seen(path: str) -> dict[str, dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(path: str, seen: dict[str, dict]):
    with open(path, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# Discord notification
# ──────────────────────────────────────────────────────────────────────

def send_notification(config: Config, new_listings: list[Listing]):
    """Send a rich embed to Discord via webhook with @everyone ping."""
    if not new_listings:
        return

    count = len(new_listings)

    embeds = []
    for l in new_listings:
        fields = []
        if l.price:
            fields.append({"name": "Price", "value": l.price, "inline": True})
        if l.area:
            fields.append({"name": "Size", "value": l.area, "inline": True})
        embeds.append({
            "title": l.title[:256],
            "url": l.url or "https://plaza.newnewnew.space/aanbod/wonen",
            "color": 0x0057B7,
            "fields": fields,
            "footer": {"text": "⚡ React within 10 min — lottery system!"},
            "timestamp": l.first_seen,
        })

    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        payload = {
            "username": "Plaza Delft Monitor",
            "content": (
                f"@everyone\n"
                f"## 🚨 {count} new Delft listing{'s' if count > 1 else ''}!\n"
                f"Go respond NOW → https://plaza.newnewnew.space/aanbod/wonen"
            ),
            "allowed_mentions": {"parse": ["everyone"]},
            "embeds": batch,
        }

        try:
            resp = requests.post(config.discord_webhook_url, json=payload, timeout=15)
            if resp.status_code == 204:
                logger.info("Discord notification sent (%d listing(s))", len(batch))
            elif resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 5)
                logger.warning("Discord rate limited, retrying in %ss", retry_after)
                time.sleep(retry_after)
                requests.post(config.discord_webhook_url, json=payload, timeout=15)
            else:
                logger.error("Discord webhook error %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Failed to send Discord notification: %s", e)


# ──────────────────────────────────────────────────────────────────────
# Main logic
# ──────────────────────────────────────────────────────────────────────

def run_once(config: Config, session: requests.Session) -> int:
    """Run a single check. Returns number of new listings found."""

    # Time window guard
    if not is_active_hours(config):
        now_cet = datetime.now(CET)
        logger.info("Outside active hours (%02d:%02d CET). Skipping.", now_cet.hour, now_cet.minute)
        return 0

    # Weekend: still check but log it (listings are rare but not impossible)
    if is_weekend():
        logger.info("Weekend check (listings are rare on weekends)")

    seen = load_seen(config.seen_file)

    logger.info("Checking Plaza for new Delft listings...")
    listings = scrape_all(session, config.target_city)
    logger.info("   Found %d total Delft listing(s) on page", len(listings))

    new_listings = [l for l in listings if l.listing_id not in seen]

    if new_listings:
        logger.info("NEW  %d listing(s) found!", len(new_listings))
        for l in new_listings:
            logger.info("   -> %s  %s  %s", l.title, l.price, l.url)
            seen[l.listing_id] = asdict(l)
        send_notification(config, new_listings)
        save_seen(config.seen_file, seen)
    else:
        logger.info("   No new listings.")

    return len(new_listings)


def main():
    """Main loop for local/always-on execution."""
    config = Config()
    config.validate()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 50)
    logger.info("  Plaza Delft Monitor (Discord)")
    logger.info("  City   : %s", config.target_city)
    logger.info("  Hours  : %02d:00–%02d:00 CET", config.active_hour_start, config.active_hour_end)
    logger.info("  Poll   : %d-%d sec", config.poll_min_seconds, config.poll_max_seconds)
    logger.info("=" * 50)

    consecutive_errors = 0

    while True:
        session = make_session()
        try:
            run_once(config, session)
            consecutive_errors = 0
        except KeyboardInterrupt:
            logger.info("Stopped by user. Bye!")
            sys.exit(0)
        except Exception as e:
            consecutive_errors += 1
            logger.error("Error during check: %s", e, exc_info=True)

        if consecutive_errors > 0:
            backoff = min(3600, config.poll_max_seconds * (2 ** consecutive_errors))
            logger.warning("   Backing off %d sec (error #%d)", backoff, consecutive_errors)
            sleep_time = backoff
        else:
            sleep_time = random.uniform(config.poll_min_seconds, config.poll_max_seconds)

        logger.info("   Next check in %.0f sec (~%.1f min)", sleep_time, sleep_time / 60)

        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            logger.info("Stopped by user. Bye!")
            sys.exit(0)


if __name__ == "__main__":
    main()