#!/usr/bin/env python3
"""
Auction Scanner
Checks auction sites for listings that match your "watches" (item type, price,
location, condition) and sends a free push notification to your phone via ntfy.

Usage:
  python scanner.py                  # normal run: scan, notify, save state
  python scanner.py --test           # dry run: show what matched and why, no alerts
  python scanner.py --test-notify    # send one test notification to your phone
  python scanner.py --inspect URL    # help find CSS selectors for a new site
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
SEEN_FILE = STATE_DIR / "seen.json"
GEO_FILE = STATE_DIR / "geocache.json"
HEALTH_FILE = STATE_DIR / "health.json"
CONFIG_FILE = ROOT / "config.yaml"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SEEN_TTL_DAYS = 90          # forget alerted listings after this long
BROKEN_SOURCE_RUNS = 12     # warn if a source finds nothing this many runs in a row

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}
STATE_ABBRS = set(US_STATES.values())


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"Missing {CONFIG_FILE.name}")
    cfg = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    cfg.setdefault("settings", {})
    problems = []
    if not cfg.get("watches"):
        problems.append("config.yaml has no 'watches'")
    for i, w in enumerate(cfg.get("watches") or []):
        if not w.get("name"):
            problems.append(f"watch #{i + 1} has no name")
    for i, s in enumerate(cfg.get("sources") or []):
        label = s.get("name") or f"source #{i + 1}"
        if not s.get("name"):
            problems.append(f"{label}: missing name")
        if not s.get("search_url"):
            problems.append(f"{label}: missing search_url")
        sel = s.get("selectors") or {}
        if not sel.get("item") or not sel.get("title"):
            problems.append(f"{label}: selectors need at least 'item' and 'title'")
    if problems:
        sys.exit("Config problems:\n  - " + "\n  - ".join(problems))
    return cfg


# --------------------------------------------------------------------------- #
# Fetching pages
# --------------------------------------------------------------------------- #

class Fetcher:
    """Gets page HTML. Uses a real headless browser for JavaScript-heavy sites."""

    def __init__(self):
        self._pw = None
        self._browser = None

    def get(self, url: str, render: bool = True, wait_for: str | None = None) -> str:
        if url.startswith("file://"):
            return Path(url[7:]).read_text()
        if not render:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            return r.text
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        page = self._browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=20_000)
                except Exception:
                    pass  # parse whatever loaded; the zero-results check will flag it
            else:
                page.wait_for_timeout(5_000)
            for _ in range(3):  # scroll to trigger lazy-loaded results
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            return page.content()
        finally:
            page.close()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


# --------------------------------------------------------------------------- #
# Parsing listings
# --------------------------------------------------------------------------- #

@dataclass
class Listing:
    source: str
    id: str
    title: str
    url: str
    price: float | None
    price_text: str
    location: str
    description: str
    ends: str
    detail_checked: bool = False


def clean(text: str) -> str:
    return " ".join(text.split())


def text_of(el, selector: str | None) -> str:
    if not selector:
        return ""
    node = el if selector == "@self" else el.select_one(selector)
    return clean(node.get_text(" ", strip=True)) if node else ""


def parse_price(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text) or re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_listings(html: str, source: dict, page_url: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    sel = source["selectors"]
    out = []
    for el in soup.select(sel["item"]):
        title = text_of(el, sel["title"])
        link_sel = sel.get("link")
        if link_sel:
            link_el = el if link_sel == "@self" else el.select_one(link_sel)
        else:
            link_el = el if el.name == "a" else el.select_one("a[href]")
        href = link_el.get("href") if link_el else None
        if not title or not href:
            continue
        url = urljoin(page_url, href).split("#")[0]
        price_text = text_of(el, sel.get("price"))
        out.append(Listing(
            source=source["name"],
            id=hashlib.sha1(url.encode()).hexdigest()[:16],
            title=title,
            url=url,
            price=parse_price(price_text),
            price_text=price_text,
            location=text_of(el, sel.get("location")),
            description=text_of(el, sel.get("description")),
            ends=text_of(el, sel.get("ends")),
        ))
    return out


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

def extract_state(text: str) -> str | None:
    if not text:
        return None
    for m in re.finditer(r"\b([A-Z]{2})\b", text):
        if m.group(1) in STATE_ABBRS:
            return m.group(1)
    low = text.lower()
    for name, abbr in US_STATES.items():
        if re.search(rf"\b{name}\b", low):
            return abbr
    return None


def location_query(text: str) -> str:
    z = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
    if z:
        return z.group(1)
    m = re.search(r"([A-Za-z][A-Za-z .'-]*),\s*([A-Z]{2})\b", text)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"
    return text.strip()


def miles_between(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class Geocoder:
    """Free geocoding via OpenStreetMap (1 request/second), cached between runs."""

    def __init__(self):
        self.cache = load_json(GEO_FILE, {})
        self._last = 0.0
        self.failures = 0

    def lookup(self, query: str):
        key = query.strip().lower()
        if not key:
            return None
        if key in self.cache:
            v = self.cache[key]
            return tuple(v) if v else None
        if self.failures >= 3:  # service unreachable; don't stall the run
            return None
        wait = 1.1 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
                headers={"User-Agent": "personal-auction-scanner/1.0"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            result = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
        except Exception as e:
            self.failures += 1
            log(f"  (geocode failed for '{query}': {e})")
            return None
        self.cache[key] = list(result) if result else None
        return result

    def save(self) -> None:
        save_json(GEO_FILE, self.cache)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def phrase_in(phrase: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text) is not None


def first_hit(text: str, words) -> str | None:
    for w in words or []:
        if phrase_in(w, text):
            return w
    return None


def evaluate(lst: Listing, watch: dict, settings: dict, geo: Geocoder, home):
    """Returns (matched: bool, detail: str). Detail is the skip reason or distance note."""
    title = lst.title.lower()
    full = f"{lst.title} {lst.description}".lower()

    include = watch.get("include_any") or []
    if include and not first_hit(title, include):
        return False, "not this item type"

    excludes = (watch.get("exclude_any") or []) + (settings.get("exclude_everywhere") or [])
    hit = first_hit(full, excludes)
    if hit:
        return False, f"excluded word '{hit}'"

    if lst.price is None:
        if watch.get("require_price"):
            return False, "no price shown"
    else:
        if watch.get("min_price") is not None and lst.price < watch["min_price"]:
            return False, f"price ${lst.price:,.0f} under min"
        if watch.get("max_price") is not None and lst.price > watch["max_price"]:
            return False, f"price ${lst.price:,.0f} over max"

    cond = watch.get("condition") or {}
    bad = first_hit(full, cond.get("reject_any"))
    if bad:
        return False, f"condition '{bad}'"
    need = cond.get("require_any") or []
    if need and not first_hit(full, need):
        return False, "no required condition words"

    allow_unknown = settings.get("allow_unknown_location", True)
    states = [s.upper() for s in watch.get("states") or []]
    if states:
        st = extract_state(lst.location)
        if st is None and not allow_unknown:
            return False, "state unknown"
        if st is not None and st not in states:
            return False, f"state {st} not allowed"

    note = ""
    radius = watch.get("radius_miles")
    if radius and home:
        pt = geo.lookup(location_query(lst.location)) if lst.location else None
        if pt is None:
            if not allow_unknown:
                return False, "distance unknown"
            note = "distance unknown"
        else:
            dist = miles_between(home, pt)
            if dist > radius:
                return False, f"{dist:.0f} mi away"
            note = f"{dist:.0f} mi"
    return True, note


# --------------------------------------------------------------------------- #
# Notifications (ntfy: free, no account needed)
# --------------------------------------------------------------------------- #

def send_push(settings: dict, title: str, message: str, url: str | None = None,
              priority: int = 3, tags=None) -> None:
    topic = os.environ.get("NTFY_TOPIC") or settings.get("ntfy_topic")
    if not topic:
        sys.exit("No ntfy topic set. Add the NTFY_TOPIC secret (see README).")
    server = (settings.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open listing", "url": url}]
    if tags:
        payload["tags"] = tags
    r = requests.post(server, json=payload, timeout=15)
    r.raise_for_status()


def alert_for(lst: Listing, watch: dict, note: str, settings: dict) -> None:
    price = f"${lst.price:,.0f}" if lst.price is not None else (lst.price_text or "no price")
    lines = [lst.title]
    where = " · ".join(x for x in (lst.location, note) if x)
    if where:
        lines.append(where)
    if lst.ends:
        lines.append(f"Ends: {lst.ends}")
    lines.append(f"via {lst.source}")
    send_push(settings, f"{watch['name']} · {price}", "\n".join(lines), url=lst.url,
              priority=watch.get("priority", 4), tags=["moneybag"])


# --------------------------------------------------------------------------- #
# Main scan
# --------------------------------------------------------------------------- #

def watches_for(source: dict, watches: list[dict]) -> list[dict]:
    return [w for w in watches if not w.get("sources") or source["name"] in w["sources"]]


def collect(source: dict, watches: list[dict], fetcher: Fetcher, delay: float) -> dict[str, Listing]:
    template = source["search_url"]
    if "{query}" in template:
        terms = []
        for w in watches:
            for t in w.get("search_terms") or [w["name"]]:
                if t not in terms:
                    terms.append(t)
        urls = [template.replace("{query}", quote_plus(t)) for t in terms]
    else:
        urls = [template]

    found: dict[str, Listing] = {}
    for url in urls:
        try:
            html = fetcher.get(url, render=source.get("render", True), wait_for=source.get("wait_for"))
            items = parse_listings(html, source, url)
            log(f"  {len(items):>3} listings  {url}")
            for it in items:
                found.setdefault(it.id, it)
        except Exception as e:
            log(f"  ERROR fetching {url}: {e}")
        time.sleep(delay)
    return found


def fill_details(lst: Listing, source: dict, fetcher: Fetcher) -> None:
    """Optionally open the listing page to read its full description."""
    lst.detail_checked = True
    sel = source.get("detail_description")
    if not sel:
        return
    try:
        html = fetcher.get(lst.url, render=source.get("render", True))
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(sel)
        if node:
            lst.description = clean(node.get_text(" ", strip=True))[:4000]
    except Exception as e:
        log(f"  (couldn't open details for {lst.url}: {e})")


def run(test: bool = False) -> None:
    cfg = load_config()
    settings = cfg["settings"]
    watches = [w for w in cfg["watches"] if w.get("enabled", True)]
    sources = [s for s in cfg.get("sources") or [] if s.get("enabled", True)]
    if not sources:
        sys.exit("No enabled sources in config.yaml")
    delay = float(settings.get("request_delay_seconds", 3))

    now = time.time()
    seen = {k: v for k, v in load_json(SEEN_FILE, {}).items() if now - v < SEEN_TTL_DAYS * 86400}
    health = load_json(HEALTH_FILE, {})
    geo = Geocoder()
    home = geo.lookup(location_query(settings["home"])) if settings.get("home") else None
    if settings.get("home") and home is None:
        log(f"WARNING: couldn't locate home '{settings['home']}'; radius filters are off this run")

    fetcher = Fetcher()
    matches = []
    try:
        for source in sources:
            relevant = watches_for(source, watches)
            if not relevant:
                continue
            log(f"\n== {source['name']} ==")
            listings = collect(source, relevant, fetcher, delay)

            streak = 0 if listings else health.get(source["name"], 0) + 1
            health[source["name"]] = streak
            if streak == BROKEN_SOURCE_RUNS and not test:
                send_push(settings, f"Scanner: {source['name']} may be broken",
                          f"No listings found for {streak} runs in a row. The site may have "
                          "changed its layout; run the 'inspect' mode to fix the selectors.",
                          priority=2, tags=["warning"])

            for lst in listings.values():
                if lst.id in seen and not test:
                    continue
                for watch in relevant:
                    ok, note = evaluate(lst, watch, settings, geo, home)
                    if ok and source.get("detail_description") and not lst.detail_checked:
                        fill_details(lst, source, fetcher)
                        ok, note = evaluate(lst, watch, settings, geo, home)
                    if test:
                        price = f"${lst.price:,.0f}" if lst.price is not None else "  ?"
                        tag = "MATCH" if ok else "skip "
                        log(f"  [{tag}] {watch['name'][:18]:<18} {price:>8}  "
                            f"{lst.title[:60]}  -- {note or lst.location}")
                    if ok:
                        matches.append((lst, watch, note))
                        break
    finally:
        fetcher.close()
        geo.save()

    if test:
        log(f"\nTest run finished: {len(matches)} match(es). No notifications sent.")
        return

    cap = int(settings.get("max_alerts_per_run", 15))
    sent = 0
    for lst, watch, note in matches[:cap]:
        try:
            alert_for(lst, watch, note, settings)
            seen[lst.id] = now
            sent += 1
        except Exception as e:
            log(f"  ERROR sending alert: {e}")
    extra = len(matches) - cap
    if extra > 0:
        send_push(settings, "More matches found",
                  f"{extra} more listing(s) matched. They'll arrive on the next run.",
                  priority=2)

    save_json(SEEN_FILE, seen)
    save_json(HEALTH_FILE, health)
    log(f"\nDone: {len(matches)} new match(es), {sent} alert(s) sent.")


# --------------------------------------------------------------------------- #
# Inspect mode: helps set up a new site
# --------------------------------------------------------------------------- #

def inspect(url: str, render: bool) -> None:
    fetcher = Fetcher()
    try:
        html = fetcher.get(url, render=render)
    finally:
        fetcher.close()
    out = ROOT / "inspect.html"
    out.write_text(html)
    soup = BeautifulSoup(html, "html.parser")

    groups: dict[str, list] = {}
    for el in soup.find_all(True):
        classes = [c for c in el.get("class") or [] if re.fullmatch(r"[A-Za-z_-][\w-]*", c)]
        if not classes:
            continue
        text = clean(el.get_text(" ", strip=True))
        if "$" in text and el.find("a", href=True) is not None and len(text) < 1500:
            groups.setdefault(f"{el.name}.{'.'.join(classes[:2])}", []).append(el)

    ranked = sorted((kv for kv in groups.items() if len(kv[1]) >= 3), key=lambda kv: -len(kv[1]))
    log(f"Saved page to {out.name} ({len(html):,} characters)\n")
    if not ranked:
        log("No repeated listing cards with prices were found. The page may need "
            "render mode, a login, or it may block automated browsers.")
        return
    log("Likely 'item' selectors (repeated elements that contain a price and a link):")
    for sel, els in ranked[:8]:
        log(f"\n  {sel}   x{len(els)}")
        for e in els[:2]:
            log(f"      e.g. {clean(e.get_text(' ', strip=True))[:110]}")
    best = ranked[0][1][0]
    log("\nHTML of the first card of the top candidate (paste this to Claude to get selectors):\n")
    log(str(best)[:3000])


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan auction sites and push matches to your phone.")
    ap.add_argument("--test", action="store_true", help="dry run; show matches and reasons")
    ap.add_argument("--test-notify", action="store_true", help="send one test notification")
    ap.add_argument("--inspect", metavar="URL", help="analyze a search page to find selectors")
    ap.add_argument("--render", action="store_true", help="with --inspect, use a real browser")
    args = ap.parse_args()

    if args.inspect:
        inspect(args.inspect, render=args.render)
    elif args.test_notify:
        cfg = load_config()
        send_push(cfg["settings"], "Auction scanner is connected",
                  "Test notification. Real alerts will look like this, with a button to open the listing.",
                  url="https://ntfy.sh", tags=["white_check_mark"])
        log("Test notification sent.")
    else:
        run(test=args.test)


if __name__ == "__main__":
    main()
