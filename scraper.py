#!/usr/bin/env python3
"""Watch Warp and Bleep for anything new about Boards of Canada.

On every run we fetch the BoC artist pages on both sites and extract
every anchor whose path sits under a "BoC-related" space (releases,
products, news, videos, ...). We diff that set against a persisted
state file and push any new entries via ntfy.sh. Each new URL is
classified at push time as music / merch / news / update so the
notification title tells you what kind of thing it is.

The first run per source only records a baseline and does not notify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

STATE_FILE = Path(__file__).with_name("state.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# Keys in state.json that are NOT sources.
RESERVED_KEYS = {"_telemetry"}


# Warp exposes each release at /releases/<id>-<slug> with optional sub-pages
# /tracklist, /reviews, /credits. Collapse them to the canonical base URL so
# we get a single stable identifier per release.
_WARP_RELEASE_RE = re.compile(r"^(https?://warp\.net/releases/[^/?#]+)(?:/.*)?$")


def _warp_canonical(url: str) -> str:
    m = _WARP_RELEASE_RE.match(url)
    return m.group(1) if m else url


def _identity(url: str) -> str:
    return url


def _title_from_slug(url: str, artist_slug: str | None) -> str:
    """Build a human title from the last URL path segment.

    For Bleep, anchors wrap only an <img>, so anchor.get_text() returns
    nothing and we'd end up showing the raw URL in push notifications.
    The URL slug already contains everything we need: drop the leading
    numeric ID, drop the embedded artist slug if there is one, titlecase
    the rest. /release/141387-boards-of-canada-peel-session becomes
    "Peel Session".
    """
    last = urlparse(url).path.rstrip("/").split("/")[-1]
    parts = [p for p in last.split("-") if p]
    if parts and parts[0].isdigit():
        parts = parts[1:]
    if artist_slug:
        slug_words = artist_slug.split("-")
        if parts[: len(slug_words)] == slug_words:
            parts = parts[len(slug_words):]
    return " ".join(p.capitalize() for p in parts) or last


# Substrings that identify an item as merchandise (wearables, physical
# goods) rather than music. Used at classification time to label pushes.
MERCH_SLUG_SUBSTRINGS = (
    "t-shirt",
    "tshirt",
    "sweatshirt",
    "hoodie",
    "longsleeve",
    "long-sleeve",
    "beanie",
    "cap-",
    "-cap",
    "tote",
    "poster",
    "sticker",
    "patch",
    "pin-",
    "-pin",
    "mug",
    "keychain",
    "tote-bag",
    "mask",
    "slipmat",
    "gift-card",
    "bundle",
)


# Path prefix → category. Checked in order; first match wins.
CATEGORY_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/news/", "news"),
    ("/article/", "news"),
    ("/articles/", "news"),
    ("/features/", "news"),
    ("/feature/", "news"),
    ("/journal/", "news"),
    ("/editorial/", "news"),
    ("/video/", "video"),
    ("/videos/", "video"),
    ("/products/", "merch"),
    ("/tour/", "tour"),
    ("/releases/", "music"),
)


def classify_url(url: str) -> str:
    """Return a short category label for a matching URL.

    We check the URL path against a set of known prefixes first. If the
    path is still ambiguous (e.g. Bleep's /release/ URL space that
    contains both music and merch), fall back to the merch keyword scan.
    """
    path = urlparse(url).path.lower()
    for prefix, label in CATEGORY_PATH_PREFIXES:
        if prefix in path:
            if label == "music" and any(s in path for s in MERCH_SLUG_SUBSTRINGS):
                return "merch"
            return label
    if "/release/" in path:
        if any(s in path for s in MERCH_SLUG_SUBSTRINGS):
            return "merch"
        return "music"
    return "update"


@dataclass
class Source:
    name: str
    url: str
    # Tuple of path substrings — an anchor matches if its URL path contains
    # ANY of these. Widen this to add new content types (news, tour, ...).
    path_markers: tuple[str, ...]
    # Optional extra filter: the URL path must contain this string too.
    # Used to scope Bleep results to BoC only (its /release/ URLs embed the
    # artist slug: /release/<id>-<artist-slug>-<album-slug>).
    required_slug: str | None = None
    # If True, derive the title from the URL slug instead of any anchor
    # text. Needed for Bleep where the text is either empty (image-only
    # anchor) or a format selector label like "LP Download".
    title_from_slug: bool = False
    # Applied to every matching URL before diffing / storing, so different
    # sub-pages of the same release collapse together.
    canonicalize: Callable[[str], str] = _identity

    def fetch(self) -> tuple[str, int, str]:
        """Return (html, http_status, final_url) after retry/backoff."""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                resp = requests.get(self.url, headers=headers, timeout=30)
                resp.raise_for_status()
                return resp.text, resp.status_code, resp.url
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    def extract_releases(self, html: str) -> dict[str, str]:
        """Return {absolute_url: title} for anchors matching any path marker."""
        soup = BeautifulSoup(html, "html.parser")
        parsed = urlparse(self.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        artist_path = parsed.path.rstrip("/")

        releases: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not any(marker in href for marker in self.path_markers):
                continue
            abs_url = urljoin(base, href).split("?")[0].split("#")[0]
            path = urlparse(abs_url).path
            # Skip the artist page itself and obvious index pages.
            if path.rstrip("/") == artist_path:
                continue
            if any(path.rstrip("/") == m.rstrip("/") for m in self.path_markers):
                continue
            if self.required_slug and self.required_slug not in path:
                continue
            abs_url = self.canonicalize(abs_url)
            if self.title_from_slug:
                title = _title_from_slug(abs_url, self.required_slug)
            else:
                title = anchor.get_text(" ", strip=True)
                if not title:
                    title = _title_from_slug(abs_url, self.required_slug)
            # Prefer the longest text node seen for the same URL, which is
            # usually the one containing the actual title rather than a
            # thumbnail-only link.
            if abs_url not in releases or len(title) > len(releases[abs_url]):
                releases[abs_url] = title
        return releases

    def telemetry(self, html: str) -> dict:
        """Per-run telemetry written back to state.json.

        Also dumps a compact inventory of anchor path prefixes on the page
        so we can see — without access to Action logs — what URL spaces
        exist and decide whether our path_markers cover them.
        """
        soup = BeautifulSoup(html, "html.parser")
        anchors = [a.get("href", "") for a in soup.find_all("a", href=True)]
        base_netloc = urlparse(self.url).netloc

        # Bucket anchors by their first two path segments. That's enough
        # granularity to tell "/news/..." from "/releases/..." from
        # "/artists/..." while staying compact.
        prefix_counts: dict[str, int] = {}
        prefix_samples: dict[str, str] = {}
        for href in anchors:
            abs_url = urljoin(f"https://{base_netloc}", href).split("?")[0].split("#")[0]
            p = urlparse(abs_url)
            if p.netloc != base_netloc:
                continue
            segments = [s for s in p.path.split("/") if s][:2]
            prefix = "/" + "/".join(segments) + "/" if segments else "/"
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
            prefix_samples.setdefault(prefix, abs_url)

        # Only keep the top ~12 prefixes by count for compactness.
        top = sorted(prefix_counts.items(), key=lambda kv: -kv[1])[:12]
        prefix_inventory = {
            pref: {"count": count, "example": prefix_samples[pref]}
            for pref, count in top
        }

        return {
            "total_anchors_on_page": len(anchors),
            "html_length": len(html),
            "looks_like_cloudflare_challenge": (
                "cf-challenge" in html.lower()
                or "just a moment" in html.lower()[:2000]
            ),
            "path_prefix_inventory": prefix_inventory,
        }


SOURCES: list[Source] = [
    Source(
        name="Warp",
        url="https://warp.net/artists/boards-of-canada/",
        # The BoC artist page on warp.net only shows BoC-related content in
        # these URL spaces, so we can trust any anchor under them without a
        # slug check. /news/ is a guess — the first run's telemetry will
        # confirm whether it exists on the artist page.
        path_markers=(
            "/releases/",
            "/products/",
            "/news/",
            "/videos/",
            "/video/",
            "/tour/",
        ),
        canonicalize=_warp_canonical,
    ),
    Source(
        # Bleep artist ID 48 is A Guy Called Gerald — the correct BoC ID is
        # 78 (confirmed via cross-link from warp.net).
        name="Bleep",
        url="https://bleep.com/artist/78-boards-of-canada",
        # Bleep lumps music and merch under /release/. News/editorial
        # might live under /news/, /features/, or /articles/ — first run's
        # telemetry will tell us which (if any).
        path_markers=(
            "/release/",
            "/news/",
            "/features/",
            "/feature/",
            "/articles/",
            "/article/",
            "/journal/",
        ),
        required_slug="boards-of-canada",
        title_from_slug=True,
    ),
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify(topic: str, title: str, message: str, click_url: str | None = None) -> requests.Response:
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
        "Tags": "loud_sound,musical_note",
    }
    if click_url:
        headers["Click"] = click_url
    resp = requests.post(
        f"{NTFY_SERVER.rstrip('/')}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not send notifications or write state.json.",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Send one test push to prove the pipeline works.",
    )
    args = parser.parse_args()

    topic = os.environ.get("NTFY_TOPIC")
    if not topic and not args.dry_run:
        print("ERROR: NTFY_TOPIC env var not set", file=sys.stderr)
        return 2

    state = load_state()
    telemetry: dict[str, dict] = {}
    new_items: list[tuple[Source, str, str]] = []

    # Consume a one-shot test-push marker. Any agent with repo write access
    # can set state["_request_test_push"] = true and push; the next run will
    # fire a test push to ntfy, capture the response status, write it to
    # _telemetry.last_test_push, and remove the marker. This is the only
    # way to verify the ntfy pipeline end-to-end without access to the
    # Actions log or workflow_dispatch triggering.
    marker_requests_push = bool(state.pop("_request_test_push", False))
    if marker_requests_push:
        print("[test-push] marker found in state.json, will send test push")

    for source in SOURCES:
        is_first_run = source.name not in state
        previous = set(state.get(source.name, {}).get("releases", {}).keys())
        tele: dict = {"fetch": "unknown"}

        try:
            html, status, final_url = source.fetch()
            tele["fetch"] = "ok"
            tele["http_status"] = status
            tele["final_url"] = final_url
        except requests.RequestException as exc:
            tele["fetch"] = f"failed: {exc}"
            print(f"[{source.name}] fetch failed: {exc}", file=sys.stderr)
            telemetry[source.name] = tele
            continue

        tele.update(source.telemetry(html))
        releases = source.extract_releases(html)
        tele["matched_count"] = len(releases)
        telemetry[source.name] = tele

        print(f"[{source.name}] parsed {len(releases)} release link(s)")

        if not releases:
            print(
                f"[{source.name}] no releases parsed — leaving previous "
                "release state untouched so we do not spam on a transient blip",
                file=sys.stderr,
            )
            continue

        added = set(releases.keys()) - previous
        if added and not is_first_run:
            for url in sorted(added):
                new_items.append((source, url, releases[url]))
        elif added and is_first_run:
            print(f"[{source.name}] baseline captured ({len(added)} releases)")

        state[source.name] = {
            "releases": releases,
            "last_checked": int(time.time()),
        }

    state["_telemetry"] = {
        "last_run": int(time.time()),
        "sources": telemetry,
    }
    # Drop old diagnostics key from previous scraper versions.
    state.pop("_diagnostics", None)

    if (args.force_notify or marker_requests_push) and not args.dry_run and topic:
        try:
            resp = notify(
                topic,
                "BoC watcher test",
                f"Test notification — scraper pipeline verified at {int(time.time())}",
            )
            # IMPORTANT: do NOT store the response body — ntfy echoes the
            # topic name back in its JSON payload, and state.json is in a
            # public repo. Status code + boolean is enough.
            state["_telemetry"]["last_test_push"] = {
                "sent_at": int(time.time()),
                "ntfy_http_status": resp.status_code,
                "ok": True,
            }
            print(f"[test-push] ntfy responded {resp.status_code}")
        except requests.RequestException as exc:
            # Strip any URL from the error message so the topic cannot leak
            # via an exception trace either.
            err = str(exc).replace(topic, "<redacted>") if topic else str(exc)
            state["_telemetry"]["last_test_push"] = {
                "sent_at": int(time.time()),
                "error": err,
                "ok": False,
            }
            print(f"[test-push] failed (redacted): {err}", file=sys.stderr)

    for source, url, title in new_items:
        category = classify_url(url)
        print(f"NEW [{source.name}/{category}] {title} -> {url}")
        if not args.dry_run and topic:
            notify(
                topic,
                f"BoC {category} — new on {source.name}",
                f"{title}\n{url}",
                click_url=url,
            )

    if not args.dry_run:
        save_state(state)

    print(f"Done. {len(new_items)} new release(s) reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
