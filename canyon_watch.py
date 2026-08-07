#!/usr/bin/env python3
"""
Canyon XS endurance-bike availability monitor.

Watches Canyon's UK Endurace range for size-XS variants that are buyable
(InStock or PreOrder) and pushes an ntfy alert only when the set actually
changes -- never a "still available" ping.

Availability is read from each product page's JSON-LD ProductGroup, which
exposes a real per-variant (colour x size) stock status. Variants are keyed
by SKU, so "Dark Matter in stock but Pro Black sold out" is tracked
correctly as two independent things.

No browser required: Canyon's filters are server-side URL params.
"""

from __future__ import annotations

import concurrent.futures as futures
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# Canyon's filters live in the URL (Salesforce Commerce Cloud refinements):
#   pc_rahmengroesse = frame size      pc_ride_style = ride style
#   pc_familie       = model family    pc_welt       = world (Road/Gravel/...)
CATEGORY_URL = os.environ.get(
    "CANYON_CATEGORY_URL",
    "https://www.canyon.com/en-gb/road-bikes/endurance-bikes/"
    "?prefn1=pc_rahmengroesse&prefv1=XS&sz=100",
)

# Frame sizes to alert on, as Canyon labels them.
TARGET_SIZES = {
    s.strip() for s in os.environ.get("CANYON_SIZES", "XS").split(",") if s.strip()
}

# schema.org availability values considered "worth telling me about".
ACCEPT_AVAILABILITY = {
    s.strip()
    for s in os.environ.get("CANYON_AVAILABILITY", "InStock,PreOrder").split(",")
    if s.strip()
}

STATE_FILE = Path(os.environ.get("CANYON_STATE_FILE", "state.json"))

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()  # optional, for private servers

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 45
MAX_RETRIES = 3
PARALLEL_FETCHES = 5

# If the listing page yields fewer than this many products, assume Canyon
# changed their markup rather than believing the whole range vanished.
MIN_PLAUSIBLE_PRODUCTS = 3

# Don't spam breakage alerts: only warn after this many consecutive failures.
FAILURES_BEFORE_ALERT = 3


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext:
    """Use certifi when the system trust store is unusable (macOS python.org)."""
    try:
        import certifi  # noqa: PLC0415

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_CTX = _ssl_context()


def fetch(url: str) -> str:
    """GET a URL as text, with retries and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-GB,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_CTX) as r:
                if r.status != 200:
                    raise urllib.error.HTTPError(url, r.status, "bad status", r.headers, None)
                return r.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {url} ({last_error})")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def product_links(listing_html: str) -> list[str]:
    """Extract product-detail-page URLs from a category listing.

    Scoped to product tiles: a bare href sweep also drags in ~13 footer and
    customer-service pages, and could pull in cross-sell products from other
    categories.
    """
    found = re.findall(r'productTileLink[^>]*href="([^"]+)"', listing_html)
    if not found:  # markup changed - fall back to category-path filtering
        found = re.findall(
            r'href="(https://www\.canyon\.com/en-gb/[a-z-]+-bikes/[^"]*?\.html)',
            listing_html,
        )
    seen: dict[str, None] = {}
    for url in found:
        if url.startswith("http"):
            seen.setdefault(url.split("?")[0], None)
    return list(seen)


def colour_names(page_html: str, page_url: str, all_codes: set[str]) -> dict[str, str]:
    """Map Canyon colour codes (R148_P03) to human names (Dark Matter)."""
    mapping: dict[str, str] = {}
    for block in re.findall(r"<button[^>]*js-product-color[^>]*>", page_html):
        title = re.search(r'title="([^"]+)"', block)
        code = re.search(r"pv_rahmenfarbe=([A-Z0-9_]+)", block)
        if title and code:
            mapping[code.group(1)] = html.unescape(title.group(1)).strip()

    # The *currently selected* swatch's data-url omits the colour param, so it
    # never maps above. Its name is in the picker heading; pair the two by
    # elimination when exactly one code is left unresolved.
    heading = re.search(r"js-colorPickerHeadingColor[^>]*>\s*([^<]+)", page_html)
    if heading:
        selected = html.unescape(heading.group(1)).strip()
        unmapped = [c for c in all_codes if c not in mapping]
        if len(unmapped) == 1:
            mapping[unmapped[0]] = selected
        else:
            current = re.search(r"pv_rahmenfarbe=([A-Z0-9_]+)", page_url)
            if current:
                mapping.setdefault(current.group(1), selected)
    return mapping


def _product_groups(page_html: str) -> list[dict]:
    """Parse every JSON-LD ProductGroup block on a page."""
    groups = []
    for blob in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        page_html,
        re.S,
    ):
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "ProductGroup":
            groups.append(data)
    return groups


def variants_from_pdp(page_html: str, page_url: str) -> list[dict]:
    """Pull matching size/availability variants out of a product page."""
    groups = _product_groups(page_html)

    # Resolve colour names first: the mapping needs to know every colour code
    # this product offers in order to identify the selected swatch.
    all_codes: set[str] = set()
    for data in groups:
        for variant in data.get("hasVariant", []):
            offer = variant.get("offers")
            if isinstance(offer, dict):
                code = re.search(r"pv_rahmenfarbe=([A-Z0-9_]+)", offer.get("url", ""))
                if code:
                    all_codes.add(code.group(1))

    colours = colour_names(page_html, page_url, all_codes)
    results: list[dict] = []

    for data in groups:
        model = (data.get("name") or "").strip()
        for variant in data.get("hasVariant", []):
            offer = variant.get("offers")
            if not isinstance(offer, dict):
                continue

            offer_url = offer.get("url", "")
            size_match = re.search(r"pv_rahmengroesse=([^&\"]+)", offer_url)
            if not size_match or size_match.group(1) not in TARGET_SIZES:
                continue

            availability = offer.get("availability", "").rsplit("/", 1)[-1]
            if availability not in ACCEPT_AVAILABILITY:
                continue

            code_match = re.search(r"pv_rahmenfarbe=([A-Z0-9_]+)", offer_url)
            code = code_match.group(1) if code_match else ""
            sku = variant.get("sku") or f"{model}|{size_match.group(1)}|{code}"

            results.append(
                {
                    "sku": str(sku),
                    "model": model,
                    "size": size_match.group(1),
                    "colour": colours.get(code, code or "unknown"),
                    "colour_code": code,
                    "availability": availability,
                    "price": offer.get("price"),
                    "currency": offer.get("priceCurrency", "GBP"),
                    "url": offer_url or page_url,
                    "source_page": page_url,
                }
            )
    return results


def scrape() -> tuple[dict[str, dict], set[str]]:
    """Return (items by SKU, set of product pages fetched successfully)."""
    log(f"Fetching listing: {CATEGORY_URL}")
    listing = fetch(CATEGORY_URL)
    links = product_links(listing)
    log(f"Found {len(links)} product pages")

    if len(links) < MIN_PLAUSIBLE_PRODUCTS:
        raise RuntimeError(
            f"only {len(links)} product links found (expected >= "
            f"{MIN_PLAUSIBLE_PRODUCTS}); markup may have changed"
        )

    items: dict[str, dict] = {}
    ok_pages: set[str] = set()
    failed: list[str] = []

    def worker(url: str):
        return url, fetch(url)

    with futures.ThreadPoolExecutor(PARALLEL_FETCHES) as pool:
        for future in futures.as_completed([pool.submit(worker, u) for u in links]):
            try:
                url, page = future.result()
            except Exception as exc:  # noqa: BLE001
                failed.append(str(exc))
                continue
            ok_pages.add(url)
            for item in variants_from_pdp(page, url):
                items[item["sku"]] = item

    if failed:
        log(f"WARNING: {len(failed)} product page(s) failed; "
            f"'no longer available' checks suppressed for those")
        for msg in failed[:3]:
            log(f"  - {msg}")

    # A total wipeout of product pages means the run is untrustworthy.
    if not ok_pages:
        raise RuntimeError("every product page fetch failed")

    return items, ok_pages


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def notify(title: str, message: str, priority: int = 3,
           tags: str = "bike", click: str | None = None) -> None:
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set - printing instead of sending:")
        log(f"  {title}\n{message}")
        return

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        "Tags": tags,
    }
    if click:
        headers["Click"] = click
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
            r.read()
        log(f"Notified: {title}")
    except Exception as exc:  # noqa: BLE001 - never let alerting crash the run
        log(f"ERROR sending notification: {exc}")


def format_price(item: dict) -> str:
    raw = item.get("price")
    if not raw:
        return "price n/a"
    symbol = "£" if item.get("currency") == "GBP" else f"{item.get('currency')} "
    try:
        return f"{symbol}{float(raw):,.0f}"
    except (TypeError, ValueError):
        return f"{symbol}{raw}"


def describe(item: dict, with_url: bool = True) -> str:
    """One notification block. The URL deep-links to this exact colour+size."""
    lines = [
        f"{item['model']} - {item['colour']} ({item['size']})",
        f"{format_price(item)} - {item['availability']}",
    ]
    if with_url and item.get("url"):
        lines.append(item["url"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"WARNING: unreadable state file ({exc}); treating as first run")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    state = load_state()
    previous: dict[str, dict] = state.get("items", {})
    first_run = "items" not in state

    try:
        current, ok_pages = scrape()
    except Exception as exc:  # noqa: BLE001
        fails = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = fails
        state["last_error"] = str(exc)
        save_state(state)
        log(f"SCRAPE FAILED ({fails} in a row): {exc}")

        # Warn once when it first crosses the threshold, so a silently broken
        # monitor doesn't look like "no bikes available".
        if fails == FAILURES_BEFORE_ALERT:
            notify(
                "Canyon watcher needs attention",
                f"{fails} consecutive failed checks.\nLast error: {exc}",
                priority=4,
                tags="warning",
            )
        return 1

    state["consecutive_failures"] = 0
    state.pop("last_error", None)

    log(f"Matching variants right now: {len(current)}")
    for item in sorted(current.values(), key=lambda i: (i["availability"], i["model"])):
        log(f"  [{item['availability']:8}] {item['model']} - {item['colour']}")

    if first_run:
        lines = [describe(i) for i in current.values()] or ["(nothing available)"]
        notify(
            f"Canyon XS watcher started - {len(current)} available now",
            "Baseline recorded. You'll only hear from me when this changes.\n\n"
            + "\n\n".join(lines),
            priority=3,
            tags="white_check_mark",
        )
        save_state(
            {
                "items": current,
                "last_check": datetime.now(timezone.utc).isoformat(),
                "consecutive_failures": 0,
            }
        )
        return 0

    new_skus = [s for s in current if s not in previous]

    # Only treat a SKU as gone if we actually re-read its product page this
    # run -- otherwise a single fetch failure would look like a sell-out.
    gone_skus = [
        s
        for s, item in previous.items()
        if s not in current and item.get("source_page") in ok_pages
    ]

    # PreOrder -> InStock is a genuine, useful transition.
    upgraded = [
        s
        for s in current
        if s in previous
        and previous[s].get("availability") != current[s].get("availability")
    ]

    for sku in new_skus:
        item = current[sku]
        in_stock = item["availability"] == "InStock"
        notify(
            ("NOW IN STOCK" if in_stock else "Pre-order open")
            + f": {item['model']} {item['size']}",
            describe(item),
            priority=5 if in_stock else 4,
            tags="rotating_light" if in_stock else "hourglass",
            click=item["url"],
        )

    for sku in upgraded:
        before = previous[sku]["availability"]
        after = current[sku]["availability"]
        item = current[sku]
        notify(
            f"{before} -> {after}: {item['model']} {item['size']}",
            describe(item),
            priority=5 if after == "InStock" else 3,
            tags="arrows_counterclockwise",
            click=item["url"],
        )

    if gone_skus:
        body = "\n\n".join(describe(previous[s]) for s in gone_skus)
        notify(
            f"No longer available ({len(gone_skus)})",
            body,
            priority=2,
            tags="x",
        )

    if not (new_skus or gone_skus or upgraded):
        log("No change.")

    state.update(
        {
            "items": current,
            "last_check": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
