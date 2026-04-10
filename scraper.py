#!/usr/bin/env python3
"""Watch Warp Records and Bleep.com for new Boards of Canada releases.

On every run we fetch the artist pages, extract links that look like
release pages, diff them against a persisted state file and push any
new entries to an ntfy.sh topic so the user gets an iOS notification.

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
RESERVED_KEYS = {"_diagnostics"}

# Candidate path substrings used when diagnosing which URL pattern a site uses
# for its release pages. Purely for debug output.
CANDIDATE_PATH_MARKERS = (
    "/records/",
    "/release/",
    "/releases/",
    "/album/",
    "/albums/",
    "/music/",
    "/shop/",
    "/product/",
)


# Warp exposes each release at /releases/<id>-<slug> with optional sub-pages
# /tracklist, /reviews, /credits. Collapse them to the canonical base URL so
# we get a single stable identifier per release.
_WARP_RELEASE_RE = re.compile(r"^(https?://warp\.net/releases/[^/?#]+)(?:/.*)?$")


def _warp_canonical(url: str) -> str:
    m = _WARP_RELEASE_RE.match(url)
    return m.group(1) if m else url


def _identity(url: str) -> str:
    return url


# Bleep lumps music and merch into the same /release/ URL space, so we
# explicitly drop anything whose slug looks like wearable/physical goods.
# A new BoC album is *never* going to have any of these substrings in
# its URL, and the false-positive risk of missing a real release is
# therefore effectively zero.
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


@dataclass
class Source:
    name: str
    url: str
    # Substring that must appear in an anchor's href for it to count as a
    # release link. Keeps navigation / social links out of the diff.
    release_path_marker: str
    # Optional extra filter: the URL path must contain this string too.
    # Used to scope Bleep results to BoC only (its /release/ URLs embed the
    # artist slug: /release/<id>-<artist-slug>-<album-slug>).
    required_slug: str | None = None
    # If True, drop URLs whose slug looks like merchandise (t-shirts etc).
    # Needed for Bleep because it lumps merch into the same /release/ URL
    # space as music. Warp keeps merch under /products/ so doesn't need it.
    drop_merch: bool = False
    # Applied to every matching release URL before diffing / storing, so
    # different sub-pages of the same release collapse together.
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
        """Return {absolute_url: title} for release-like anchors."""
        soup = BeautifulSoup(html, "html.parser")
        parsed = urlparse(self.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        artist_path = parsed.path.rstrip("/")

        releases: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if self.release_path_marker not in href:
                continue
            abs_url = urljoin(base, href).split("?")[0].split("#")[0]
            path = urlparse(abs_url).path
            # Skip the artist page itself and obvious index pages.
            if path.rstrip("/") == artist_path:
                continue
            if path.rstrip("/") == self.release_path_marker.rstrip("/"):
                continue
            if self.required_slug and self.required_slug not in path:
                continue
            if self.drop_merch and any(s in path for s in MERCH_SLUG_SUBSTRINGS):
                continue
            abs_url = self.canonicalize(abs_url)
            title = anchor.get_text(" ", strip=True) or abs_url
            # Prefer the longest text node seen for the same URL, which is
            # usually the one containing the actual release title rather
            # than a thumbnail-only link.
            if abs_url not in releases or len(title) > len(releases[abs_url]):
                releases[abs_url] = title
        return releases

    def diagnose(self, html: str) -> dict:
        """Return debug info about what kinds of links the page contains."""
        soup = BeautifulSoup(html, "html.parser")
        anchors = [a.get("href", "") for a in soup.find_all("a", href=True)]
        parsed = urlparse(self.url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # Count matches per candidate marker, and collect a small sample.
        pattern_counts: dict[str, int] = {}
        pattern_samples: dict[str, list[str]] = {}
        for href in anchors:
            abs_url = urljoin(base, href).split("?")[0].split("#")[0]
            path = urlparse(abs_url).path
            for marker in CANDIDATE_PATH_MARKERS:
                if marker in path:
                    pattern_counts[marker] = pattern_counts.get(marker, 0) + 1
                    pattern_samples.setdefault(marker, [])
                    if len(pattern_samples[marker]) < 8:
                        if abs_url not in pattern_samples[marker]:
                            pattern_samples[marker].append(abs_url)
                    break

        # Also sample anchors that contain the artist slug "boards", in case
        # the URL structure doesn't use any of the candidate path markers.
        boards_hits: list[str] = []
        for href in anchors:
            abs_url = urljoin(base, href).split("?")[0].split("#")[0]
            if "boards" in abs_url.lower() and abs_url not in boards_hits:
                boards_hits.append(abs_url)
            if len(boards_hits) >= 15:
                break

        return {
            "total_anchors": len(anchors),
            "pattern_counts": pattern_counts,
            "pattern_samples": pattern_samples,
            "boards_hits": boards_hits,
            "html_length": len(html),
            "has_cloudflare_challenge": "cf-challenge" in html.lower()
            or "just a moment" in html.lower()[:2000],
        }


SOURCES: list[Source] = [
    Source(
        name="Warp Records",
        url="https://warp.net/artists/boards-of-canada/",
        release_path_marker="/releases/",
        canonicalize=_warp_canonical,
    ),
    Source(
        # Bleep artist ID 48 is A Guy Called Gerald — the correct BoC ID is
        # 78 (confirmed via cross-link from warp.net).
        name="Bleep",
        url="https://bleep.com/artist/78-boards-of-canada",
        release_path_marker="/release/",
        required_slug="boards-of-canada",
        drop_merch=True,
    ),
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify(topic: str, title: str, message: str, click_url: str | None = None) -> None:
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
    diagnostics: dict[str, dict] = {}
    new_items: list[tuple[Source, str, str]] = []

    for source in SOURCES:
        is_first_run = source.name not in state
        previous = set(state.get(source.name, {}).get("releases", {}).keys())
        diag: dict = {"fetch": "unknown"}

        try:
            html, status, final_url = source.fetch()
            diag["fetch"] = "ok"
            diag["http_status"] = status
            diag["final_url"] = final_url
        except requests.RequestException as exc:
            diag["fetch"] = f"failed: {exc}"
            print(f"[{source.name}] fetch failed: {exc}", file=sys.stderr)
            diagnostics[source.name] = diag
            continue

        diag.update(source.diagnose(html))
        releases = source.extract_releases(html)
        diag["matched_count"] = len(releases)
        diag["matched_sample"] = list(releases.keys())[:10]
        diagnostics[source.name] = diag

        print(
            f"[{source.name}] parsed {len(releases)} release link(s) "
            f"(total anchors: {diag['total_anchors']})"
        )

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

    state["_diagnostics"] = {
        "last_run": int(time.time()),
        "sources": diagnostics,
    }

    if args.force_notify and not args.dry_run and topic:
        notify(
            topic,
            "BoC watcher test",
            "Test notification — scraper is alive.",
        )

    for source, url, title in new_items:
        print(f"NEW [{source.name}] {title} -> {url}")
        if not args.dry_run and topic:
            notify(
                topic,
                f"BoC — new on {source.name}",
                f"{title}\n{url}",
                click_url=url,
            )

    if not args.dry_run:
        save_state(state)

    print(f"Done. {len(new_items)} new release(s) reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
