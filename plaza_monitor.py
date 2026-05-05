#!/usr/bin/env python3
"""
Plaza Resident Services — Delft Listing Monitor (Playwright Edition)
====================================================================
Uses a headless browser to wait for JavaScript to render, ensuring
we actually see the listings before parsing.
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

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CET = timezone(timedelta(hours=2))


def load_env_file():
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
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    poll_min_seconds: int = int(os.getenv("POLL_MIN_SECONDS", "300"))
    poll_max_seconds: int = int(os.getenv("POLL_MAX_SECONDS", "400"))
    active_hour_start: int = int(os.getenv("ACTIVE_HOUR_START", "7"))
    active_hour_end: int = int(os.getenv("ACTIVE_HOUR_END", "23"))
    target_city: str = os.getenv("TARGET_CITY", "delft")
    seen_file: str = os.getenv("SEEN_FILE", str(Path(__file__).parent / "seen_listings.json"))

    def validate(self):
        if not self.discord_webhook_url:
            print("\n❌ Missing DISCORD_WEBHOOK_URL. Set it in your .env file.\n")
            sys.exit(1)


def is_active_hours(config: Config) -> bool:
    now_cet = datetime.now(CET)
    return config.active_hour_start <= now_cet.hour < config.active_hour_end


@dataclass
class Listing:
    listing_id: str
    title: str = ""
    address: str = ""
    city: str = ""
    price: str = ""
    area: str = ""
    url: str = ""
    first_seen: str = ""
    raw_text: str = ""


LISTING_URLS = [
    "https://plaza.newnewnew.space/en/availables-places/living-place",
    "https://plaza.newnewnew.space/aanbod/wonen",
    "https://plaza.newnewnew.space/en/our-complexes/delft",
    "https://plaza.newnewnew.space/onze-complexen/nederland/delft"
]

logger = logging.getLogger("plaza_monitor")
_page_hashes: dict[str, str] = {}


def page_changed(url: str, html: str) -> bool:
    content_hash = hashlib.md5(html.encode()).hexdigest()
    if _page_hashes.get(url) == content_hash:
        return False
    _page_hashes[url] = content_hash
    return True


def extract_listings_from_html(html: str, target_city: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    found_lids = set()

    # Look at EVERY link on the rendered page
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        # Walk up the DOM to grab the whole property card
        card = a_tag
        for _ in range(6):
            if card.parent and card.parent.name in ["div", "li", "article", "section"]:
                card = card.parent
                if len(card.get_text(strip=True)) > 50:
                    break

        raw_text = card.get_text(separator=" | ", strip=True).lower()

        # If "delft" isn't in the card text or URL, skip it
        if target_city.lower() not in raw_text and target_city.lower() not in href.lower():
            continue

        # Ignore obvious trash links
        if "login" in href.lower() or "faq" in href.lower() or "page=" in href.lower():
            continue

        base_href = href.split("?")[0]
        if not base_href.startswith("http"):
            base_href = "https://plaza.newnewnew.space" + base_href

        lid = hashlib.sha256(base_href.encode()).hexdigest()[:16]
        if lid in found_lids:
            continue

        found_lids.add(lid)

        price_match = re.search(r"€\s*[\d.,]+", raw_text)
        area_match = re.search(r"(\d+)\s*m[²2]", raw_text)

        title_text = a_tag.get_text(separator=" ", strip=True)
        if len(title_text) < 5:
            header = card.find(["h2", "h3", "h4", "h5"])
            title_text = header.get_text(strip=True) if header else base_href.split("/")[-1]

        listings.append(Listing(
            listing_id=lid,
            title=title_text[:100].title(),
            city=target_city.capitalize(),
            price=price_match.group(0) if price_match else "Unknown",
            area=area_match.group(0) if area_match else "Unknown",
            url=base_href,
            first_seen=now_iso,
        ))

    return listings


def scrape_all(target_city: str) -> list[Listing]:
    all_listings: dict[str, Listing] = {}

    with sync_playwright() as p:
        # Launch invisible Chrome
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for url in LISTING_URLS:
            try:
                # Wait for the network to go quiet (JS is done loading)
                page.goto(url, wait_until="networkidle", timeout=30000)
                # Hard pause for 2 seconds just to be absolutely sure the DOM updated
                page.wait_for_timeout(2000)
                html = page.content()

                if html and page_changed(url, html):
                    for listing in extract_listings_from_html(html, target_city):
                        all_listings[listing.listing_id] = listing
            except Exception as e:
                logger.warning("Failed to load %s: %s", url, str(e))

            time.sleep(random.uniform(1.0, 3.0))

        browser.close()

    return list(all_listings.values())


def load_seen(path: str) -> dict[str, dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(path: str, seen: dict[str, dict]):
    with open(path, "w") as f: json.dump(seen, f, indent=2, ensure_ascii=False)


def send_notification(config: Config, new_listings: list[Listing]):
    if not new_listings: return

    embeds = []
    for l in new_listings:
        fields = []
        if l.price and l.price != "Unknown": fields.append({"name": "Price", "value": l.price, "inline": True})
        if l.area and l.area != "Unknown": fields.append({"name": "Size", "value": l.area, "inline": True})

        embeds.append({
            "title": l.title,
            "url": l.url,
            "color": 0x0057B7,
            "fields": fields,
            "footer": {"text": "⚡ Go go go!"},
            "timestamp": l.first_seen,
        })

    for i in range(0, len(embeds), 10):
        payload = {
            "username": "Plaza Monitor",
            "content": f"@everyone\n## 🚨 {len(new_listings)} new Delft listing(s)!\nhttps://plaza.newnewnew.space/en/availables-places/living-place",
            "allowed_mentions": {"parse": ["everyone"]},
            "embeds": embeds[i:i + 10],
        }
        try:
            requests.post(config.discord_webhook_url, json=payload, timeout=15)
        except Exception as e:
            logger.error("Discord error: %s", e)


def run_once(config: Config) -> int:
    if not is_active_hours(config):
        logger.info("Outside active hours. Sleeping.")
        return 0

    logger.info("Spinning up browser to check Plaza...")
    seen = load_seen(config.seen_file)
    listings = scrape_all(config.target_city)

    logger.info("   Actually found %d %s listing(s) on the screen.", len(listings), config.target_city.capitalize())

    new_listings = [l for l in listings if l.listing_id not in seen]

    if new_listings:
        logger.info("🚨 NEW %d listing(s) found! Sending to Discord...", len(new_listings))
        for l in new_listings:
            seen[l.listing_id] = asdict(l)
        send_notification(config, new_listings)
        save_seen(config.seen_file, seen)
    else:
        logger.info("   No new listings to report.")

    return len(new_listings)


def main():
    config = Config()
    config.validate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    logger.info("Starting up Brute-Force Plaza Monitor...")

    while True:
        try:
            run_once(config)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            logger.error("Error: %s", e)

        sleep_time = random.uniform(config.poll_min_seconds, config.poll_max_seconds)
        logger.info("   Next check in %.0f sec (~%.1f min)", sleep_time, sleep_time / 60)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()