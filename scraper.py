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
import sys
import time
from dataclasses import dataclass
from pathlib import Path
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


@dataclass
class Source:
    name: str
    url: str
    # Substring that must appear in an anchor's href for it to count as a
    # release link. Keeps navigation / social links out of the diff.
    release_path_marker: str

    def fetch(self) -> str:
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
                return resp.text
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
            # Skip the artist page itself and obvious index pages.
            if urlparse(abs_url).path.rstrip("/") == artist_path:
                continue
            if urlparse(abs_url).path.rstrip("/") == self.release_path_marker.rstrip("/"):
                continue
            title = anchor.get_text(" ", strip=True) or abs_url
            # Prefer the longest text node seen for the same URL, which is
            # usually the one containing the actual release title rather
            # than a thumbnail-only link.
            if abs_url not in releases or len(title) > len(releases[abs_url]):
                releases[abs_url] = title
        return releases


SOURCES: list[Source] = [
    Source(
        name="Warp Records",
        url="https://warp.net/artists/boards-of-canada/",
        release_path_marker="/records/",
    ),
    Source(
        name="Bleep",
        url="https://bleep.com/artist/48-boards-of-canada",
        release_path_marker="/release/",
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
        # ntfy reads these as ISO-8859-1; RFC 8187 encoding handles unicode.
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
    new_items: list[tuple[Source, str, str]] = []

    for source in SOURCES:
        is_first_run = source.name not in state
        previous = set(state.get(source.name, {}).get("releases", {}).keys())
        try:
            html = source.fetch()
        except requests.RequestException as exc:
            print(f"[{source.name}] fetch failed after retries: {exc}", file=sys.stderr)
            continue

        releases = source.extract_releases(html)
        print(f"[{source.name}] parsed {len(releases)} release link(s)")
        if not releases:
            # Don't wipe known state if the selector broke.
            print(
                f"[{source.name}] no releases parsed — leaving previous state untouched",
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
