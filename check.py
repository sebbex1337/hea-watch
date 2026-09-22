#!/usr/bin/env python3
"""Watch HEA's garage on Uranienborg Allé 1 and shout the moment anything moves.

The site sits behind a WAF that hands every client a JavaScript proof-of-work
challenge, so we drive a real Chromium and let it answer the challenge the same
way a browser would. Everything below assumes that page.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # lets test_check.py exercise the parsing without a browser
    PWTimeout = TimeoutError
    sync_playwright = None

LISTING_URL = "https://hea.dk/lejemaal/p-plads/garage-udlejes-pa-uranienborg-alle-i-soborg/"
CATEGORY_URL = "https://hea.dk/lejemaal/p-plads/"

STATE_PATH = Path("state.json")
DEBUG_DIR = Path("debug")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Status vocabulary. TAKEN_WORDS is used *only* to decide how loudly to shout --
# never to decide whether to shout at all. An unrecognised status word counts as
# possible good news, so a rewording on HEA's side can't silence us.
TAKEN_WORDS = {"udlejet", "optaget", "reserveret", "reserveret."}
FREE_WORDS = {"ledig", "ledigt", "ledige", "udlejes", "tilgengelig", "tilgaengelig"}
STATUS_WORDS = TAKEN_WORDS | FREE_WORDS

# The listing's own text block: starts at the breadcrumb naming the address (above
# it is HEA's site nav, which changes for marketing reasons), ends where the
# cross-sell to other listings begins.
START_MARKERS = ("uranienborg",)
END_MARKERS = ("andre lejemal", "andre lejemaal", "lignende lejemal")

# Lines that change on their own and would cause pure-noise alerts.
NOISE_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"cookie", re.I),
    re.compile(r"^\s*(cvr|tlf|telefon|copyright|©)", re.I),
    re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b"),  # bare dates
]

CHALLENGE_MARKERS = ("checking your browser", "sc-challenge", "request blocked")
GONE_MARKERS = ("siden findes ikke", "404", "not found", "findes ikke")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fold(s: str) -> str:
    """Lowercase and strip diacritics, so 'Andre Lejemål' == 'andre lejemal'."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def normalise(text: str) -> list[str]:
    """Visible text -> comparable lines, with self-changing noise dropped."""
    out = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        out.append(line)
    return out


def fingerprint(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"!! state.json unreadable ({exc}); starting fresh", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

class Blocked(Exception):
    """The WAF challenge never resolved."""


def load_page(page, url: str) -> tuple[int, str, str]:
    """Return (http_status, visible_text, html) once the WAF has let us through.

    The challenge page solves a proof-of-work in JS and then reloads itself, so
    we wait for it to disappear and then re-request to get an honest status code.
    """
    resp = page.goto(url, wait_until="load", timeout=60_000)
    status = resp.status if resp else 0

    for attempt in range(8):
        blob = fold(page.title() + " " + page.content())
        if not any(m in blob for m in CHALLENGE_MARKERS):
            break
        print(f"   waiting out WAF challenge ({attempt + 1}/8)...")
        page.wait_for_timeout(4_000)
    else:
        raise Blocked(f"challenge still showing after ~32s at {url}")

    if status >= 400:
        # We were challenged; the reload happened inside the browser, so ask
        # again now that we hold the clearance cookie.
        resp = page.goto(url, wait_until="load", timeout=60_000)
        status = resp.status if resp else 0
        if any(m in fold(page.content()) for m in CHALLENGE_MARKERS):
            raise Blocked(f"challenge reappeared on retry at {url}")

    page.wait_for_timeout(1_000)
    return status, page.inner_text("body"), page.content()


def extract_listing(text: str) -> list[str]:
    """The listing's own text, between the address breadcrumb and the cross-sell.

    If either marker is missing we widen rather than narrow: comparing too much
    risks a false alert, comparing nothing risks silence, and silence is the one
    failure mode this script exists to avoid.
    """
    lines = normalise(text)

    start = 0
    for i, line in enumerate(lines):
        if any(m in fold(line) for m in START_MARKERS):
            start = i
            break

    end = len(lines)
    for i in range(start, len(lines)):
        if any(m in fold(lines[i]) for m in END_MARKERS):
            end = i
            break

    return lines[start:end]


def find_statuses(lines: list[str]) -> list[str]:
    """Every status word on the page, deduped, in order of appearance.

    Deliberately not first-match-wins: the page says 'udlejes' in its own
    heading, so a first match would latch onto that and never see the 'udlejet'
    badge -- which would quietly disable the whole point of this script.
    """
    found: list[str] = []
    for line in lines:
        for word in fold(line).split():
            word = word.strip(".,:;()!?–-")
            if word in STATUS_WORDS and word not in found:
                found.append(word)
    return found


def is_taken(statuses: list[str]) -> bool:
    """True only if the page positively says the garage is gone.

    Absence of evidence is treated as good news, never as 'still taken'.
    """
    return any(w in TAKEN_WORDS for w in statuses)


def extract_og_updated(html: str) -> str | None:
    m = re.search(
        r'<meta[^>]+property=["\']og:updated_time["\'][^>]+content=["\']([^"\']+)',
        html, re.I,
    )
    return m.group(1) if m else None


def extract_category_items(page) -> list[str]:
    """Every listing URL the P-plads category page links to."""
    hrefs = page.eval_on_selector_all(
        "a[href*='/lejemaal/']",
        "els => els.map(e => e.href)",
    )
    return sorted({h.split("?")[0].rstrip("/") for h in hrefs if "/lejemaal/" in h})


# --------------------------------------------------------------------------- #
# diffing and notification
# --------------------------------------------------------------------------- #

def render_diff(old: list[str], new: list[str], limit: int = 3_600) -> str:
    diff = list(difflib.unified_diff(old, new, lineterm="", n=1))[2:]
    if not diff:
        return "(no visible line changes)"
    body = "\n".join(diff)
    if len(body) > limit:
        body = body[:limit] + "\n... (truncated)"
    return body


def notify(title: str, body: str, *, loud: bool, url: str = LISTING_URL) -> None:
    line = f"{'@everyone ' if loud else ''}{title}"
    print(f"\n>> NOTIFY {'(LOUD)' if loud else ''}: {title}\n{body[:800]}\n")

    if not WEBHOOK:
        print("!! DISCORD_WEBHOOK not set; nothing sent", file=sys.stderr)
        return

    payload = {
        "content": line[:1_900],
        "allowed_mentions": {"parse": ["everyone"] if loud else []},
        "embeds": [{
            "title": "Garage på Uranienborg Allé 1, Søborg",
            "url": url,
            "description": f"```diff\n{body[:3_900]}\n```" if body else "",
            "color": 0xE5484D if loud else 0x6E7681,
            "footer": {"text": f"hea-watch · {now()}"},
        }],
    }
    req = urlrequest.Request(
        WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "hea-watch"},
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as r:
            print(f"   discord: {r.status}")
    except urlerror.HTTPError as exc:
        print(f"!! discord rejected the message: {exc.code} {exc.read()[:300]!r}",
              file=sys.stderr)
    except OSError as exc:
        print(f"!! could not reach discord: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(page, state: dict) -> dict:
    alerts: list[tuple[str, str, bool]] = []

    # --- the garage itself -------------------------------------------------- #
    print(f"-> {LISTING_URL}")
    status, text, html = load_page(page, LISTING_URL)
    print(f"   HTTP {status}, {len(text)} chars of visible text")

    lines = extract_listing(text)
    fp = fingerprint(lines)
    statuses = find_statuses(lines)
    taken = is_taken(statuses)
    og = extract_og_updated(html)
    gone = status == 404 or any(m in fold(text) for m in GONE_MARKERS[:1])

    print(f"   status words: {statuses}  -> taken={taken}")
    print(f"   og:updated_time: {og!r}")
    print(f"   fingerprint: {fp[:16]}  ({len(lines)} lines)")

    prev = state.get("listing", {})
    first_run = not prev

    # A page that no longer says "taken" is the thing we are actually here for.
    free = not taken
    was_taken = bool(prev.get("taken"))

    if first_run:
        print("   first run -- recording baseline, not alerting")
    elif gone and not prev.get("gone"):
        alerts.append((
            "Listing is GONE (404). Either it was rented and pulled, or it moved.",
            f"HTTP {status}", True,
        ))
    elif fp != prev.get("fingerprint"):
        loud = free and was_taken
        head = (
            "STATUS CHANGED — the garage may be available!"
            if loud else
            f"Listing text changed (status still reads {', '.join(statuses) or 'unknown'})"
        )
        alerts.append((head, render_diff(prev.get("lines", []), lines), loud))
    elif og and og != prev.get("og_updated_time"):
        alerts.append((
            "HEA touched the page (og:updated_time moved) but the visible text is identical.",
            f"- {prev.get('og_updated_time')}\n+ {og}", False,
        ))
    else:
        print("   no change")

    # Builds up HEA's real status vocabulary over time, so TAKEN_WORDS can be
    # tightened later from evidence rather than guesswork.
    seen = set(state.get("seen_status_phrases", [])) | set(statuses)

    # --- the rest of the P-plads category ----------------------------------- #
    print(f"-> {CATEGORY_URL}")
    try:
        _, cat_text, _ = load_page(page, CATEGORY_URL)
        items = extract_category_items(page)
        print(f"   {len(items)} listings linked")
        prev_items = set(state.get("category", {}).get("items", []))
        if prev_items:
            new = [i for i in items if i not in prev_items]
            garages = [i for i in new if "garage" in i.lower()]
            if garages:
                alerts.append((
                    f"{len(garages)} new garage listing(s) in P-plads",
                    "\n".join(f"+ {g}" for g in garages), True,
                ))
            elif new:
                alerts.append((
                    f"{len(new)} new P-plads listing(s)",
                    "\n".join(f"+ {n}" for n in new), False,
                ))
        else:
            print("   first run -- recording baseline, not alerting")
        category = {"items": items, "checked": now()}
    except (Blocked, PWTimeout) as exc:
        print(f"!! category page failed: {exc}", file=sys.stderr)
        category = state.get("category", {})

    for title, body, loud in alerts:
        notify(title, body, loud=loud)

    return {
        "version": 1,
        "listing": {
            "http_status": status,
            "fingerprint": fp,
            "lines": lines,
            "statuses": statuses,
            "taken": taken,
            "og_updated_time": og,
            "gone": gone,
        },
        "category": category,
        "seen_status_phrases": sorted(seen),
        "first_seen": state.get("first_seen", now()),
        "last_checked": now(),
        "last_change": now() if alerts else state.get("last_change"),
        "consecutive_failures": 0,
    }


def send_test() -> int:
    """Prove the Discord wiring works, through the exact path a real alert takes.

    Sends both loudness levels: @everyone can be blocked by channel permissions
    independently of the webhook itself, and the alert you actually care about is
    the loud one.
    """
    if not WEBHOOK:
        print("!! DISCORD_WEBHOOK is empty -- the secret is missing or misnamed",
              file=sys.stderr)
        return 1

    notify(
        "Test: quiet alert. This is what a price or wording edit looks like.",
        "- this is how a removed line appears\n+ this is how an added line appears",
        loud=False,
    )
    notify(
        "Test: LOUD alert. This is what you will get if the garage frees up.",
        "- UDLEJET\n+ LEDIG",
        loud=True,
    )
    print("\nBoth test messages sent. Check the Discord channel.")
    print("If only one arrived, @everyone is blocked by that channel's permissions.")
    return 0


def main() -> int:
    if "--test" in sys.argv:
        return send_test()

    state = load_state()
    DEBUG_DIR.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="da-DK",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            new_state = run(page, state)
        except Exception as exc:  # noqa: BLE001 -- we want to report *any* breakage
            fails = state.get("consecutive_failures", 0) + 1
            print(f"!! check failed ({fails} in a row): {exc!r}", file=sys.stderr)
            try:
                page.screenshot(path=str(DEBUG_DIR / "failure.png"), full_page=True)
                (DEBUG_DIR / "failure.html").write_text(page.content())
            except Exception:  # noqa: BLE001 -- best effort only
                pass
            # Don't cry wolf on a single blip, but never fail silently forever.
            if fails == 3:
                notify(
                    "hea-watch has failed 3 times in a row — it is no longer watching.",
                    f"{type(exc).__name__}: {exc}", loud=True,
                )
            state["consecutive_failures"] = fails
            state["last_checked"] = now()
            save_state(state)
            return 1
        finally:
            try:
                (DEBUG_DIR / "page.txt").write_text(page.inner_text("body"))
                page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
            except Exception:  # noqa: BLE001 -- best effort only
                pass
            browser.close()

    save_state(new_state)
    print("state.json written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
