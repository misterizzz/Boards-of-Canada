#!/usr/bin/env python3
# Bump: Apple Watch mirror verification, 2026-04-11.
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
import base64
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
DOCS_DIR = Path(__file__).with_name("docs")
EVENTS_FILE = DOCS_DIR / "events.json"
SUBSCRIPTIONS_FILE = DOCS_DIR / "subscriptions.json"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
VAPID_CLAIMS_SUB = os.environ.get(
    "VAPID_CLAIMS_SUB", "mailto:boc-watcher@users.noreply.github.com"
)
MAX_EVENTS = 500

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
    # Optional extra filter: the anchor's visible text must contain this
    # substring (case-insensitive). Used for editorial/news pages where
    # the URL doesn't embed the artist name but the article title does.
    required_text: str | None = None
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
            abs_parsed = urlparse(abs_url)
            # Only keep same-domain links — otherwise a broad path_markers
            # like ("/",) would sweep in every twitter / youtube / external
            # anchor on the page.
            if abs_parsed.netloc != parsed.netloc:
                continue
            path = abs_parsed.path
            # Skip the artist page itself and obvious index pages.
            if path.rstrip("/") == artist_path:
                continue
            if any(path.rstrip("/") == m.rstrip("/") for m in self.path_markers):
                continue
            if self.required_slug and self.required_slug not in path:
                continue
            anchor_text = anchor.get_text(" ", strip=True)
            if self.required_text and self.required_text.lower() not in anchor_text.lower():
                continue
            abs_url = self.canonicalize(abs_url)
            if self.title_from_slug:
                title = _title_from_slug(abs_url, self.required_slug)
            else:
                title = anchor_text
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
        # slug check. News doesn't live here — see Warp Editorial below.
        path_markers=(
            "/releases/",
            "/products/",
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
        # Bleep only surfaces /release/ URLs from the artist page (music +
        # merch lumped together). Its editorial/news content is elsewhere
        # on the site and is not linked from the artist page, so news
        # tracking on Bleep would need a separate source with a different
        # URL — skipped for now, no obvious endpoint.
        path_markers=("/release/",),
        required_slug="boards-of-canada",
        title_from_slug=True,
    ),
    Source(
        # Warp posts label news at warp.net/editorial. We fetch that index
        # page and keep any article whose visible anchor text mentions
        # "Boards of Canada" — the URL slugs of editorial posts don't
        # embed artist names so text filtering is the only reliable hook.
        name="Warp Editorial",
        url="https://warp.net/editorial",
        path_markers=("/editorial/",),
        required_text="boards of canada",
    ),
    Source(
        # BoC's own website. Structure is unknown at first run; we accept
        # every same-domain anchor with a non-root path and let the diff
        # do its job. No required_slug/required_text because the whole
        # site is already artist-scoped. First-run baseline captures
        # whatever is currently there; every future anchor added becomes
        # a notification.
        name="BoC Official",
        url="https://boardsofcanada.com/",
        path_markers=("/",),
    ),
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_events() -> list:
    if EVENTS_FILE.exists():
        try:
            return json.loads(EVENTS_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_events(events: list) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.write_text(json.dumps(events, indent=2) + "\n")


def load_subscriptions() -> list:
    if SUBSCRIPTIONS_FILE.exists():
        try:
            return json.loads(SUBSCRIPTIONS_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_subscriptions(subs: list) -> None:
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTIONS_FILE.write_text(json.dumps(subs, indent=2) + "\n")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _build_vapid_auth_header(
    vapid_private_key_b64url: str, endpoint: str, sub: str
) -> str:
    """Build an RFC 8292 VAPID Authorization header.

    `vapid_private_key_b64url` is the raw 32-byte ECDSA P-256 private
    scalar, base64url-encoded, exactly the format my keygen script emits
    and what we ask the user to store in the VAPID_PRIVATE_KEY secret.
    """
    import base64 as _base64  # local alias to keep global imports tidy

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    padded = vapid_private_key_b64url + "=" * ((4 - len(vapid_private_key_b64url) % 4) % 4)
    raw_private = _base64.urlsafe_b64decode(padded)
    private_value = int.from_bytes(raw_private, "big")
    priv = ec.derive_private_key(private_value, ec.SECP256R1())
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = _b64url_encode(pub_bytes)

    parsed = urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    header_segment = _b64url_encode(b'{"typ":"JWT","alg":"ES256"}')
    claims = {
        "aud": audience,
        "exp": int(time.time()) + 12 * 3600,
        "sub": sub,
    }
    claims_segment = _b64url_encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
    der_sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_segment = _b64url_encode(raw_sig)
    jwt = f"{header_segment}.{claims_segment}.{sig_segment}"
    return f"vapid t={jwt}, k={pub_b64}"


def send_web_push(
    subscriptions: list,
    vapid_private_key: str,
) -> list:
    """Fire a zero-byte "tickle" push to every subscription.

    No payload encryption, just a VAPID-signed POST with empty body. The
    service worker's `push` handler fetches events.json and shows the
    newest entry as a notification. This avoids having to implement RFC
    8291 payload encryption (aes128gcm with HKDF over p256dh+auth) which
    pywebpush normally handles, at the cost of one extra HTTP round trip
    in the service worker before the banner shows up.

    Returns the list of subscriptions that are still alive. 404/410 from
    the push service means the subscription is dead — we prune it so
    `docs/subscriptions.json` self-cleans over time.
    """
    survivors: list = []
    for sub in subscriptions:
        endpoint = sub.get("endpoint")
        if not endpoint:
            continue
        try:
            auth = _build_vapid_auth_header(
                vapid_private_key, endpoint, VAPID_CLAIMS_SUB
            )
            resp = requests.post(
                endpoint,
                headers={
                    "Authorization": auth,
                    "TTL": "3600",
                    "Content-Length": "0",
                    "Urgency": "normal",
                },
                data=b"",
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"[webpush] network error, keeping sub: {exc}", file=sys.stderr)
            survivors.append(sub)
            continue

        if resp.status_code in (200, 201, 202):
            survivors.append(sub)
        elif resp.status_code in (404, 410):
            print(
                f"[webpush] pruning dead subscription ({resp.status_code})",
                file=sys.stderr,
            )
        else:
            # Transient error, keep it and retry next run.
            print(
                f"[webpush] push returned {resp.status_code}, keeping sub: "
                f"{resp.text[:120]}",
                file=sys.stderr,
            )
            survivors.append(sub)
    return survivors


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

        print(f"[{source.name}] parsed {len(releases)} link(s)")

        # Transient-blip protection: if an ESTABLISHED source suddenly
        # returns 0 items, don't clobber its baseline — most likely a
        # temporary fetch anomaly or selector drift. But for a FIRST
        # run, an empty result set IS a legitimate baseline (e.g. Warp
        # Editorial currently has no BoC article). Writing that empty
        # baseline is important: otherwise the source stays stuck in
        # "first run" mode forever and the eventual first real match
        # would be treated as baseline instead of triggering a push.
        if not releases and not is_first_run:
            print(
                f"[{source.name}] 0 items on a non-first run — keeping "
                "stored state to avoid losing the baseline on a blip",
                file=sys.stderr,
            )
            continue

        added = set(releases.keys()) - previous
        if is_first_run:
            print(f"[{source.name}] baseline captured ({len(releases)} items)")
        elif added:
            for url in sorted(added):
                new_items.append((source, url, releases[url]))

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

    # Also fire a test tickle push over Web Push whenever the test-push
    # marker is set. This verifies the VAPID pipeline end-to-end without
    # requiring a synthesized fake release event in events.json.
    if (args.force_notify or marker_requests_push) and not args.dry_run:
        vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
        subs = load_subscriptions()
        if vapid_private and subs:
            before = len(subs)
            subs = send_web_push(subs, vapid_private)
            save_subscriptions(subs)
            state["_telemetry"]["last_test_webpush"] = {
                "sent_at": int(time.time()),
                "subscriptions_before": before,
                "subscriptions_after": len(subs),
            }
            print(
                f"[test-webpush] tickled {before} subscription(s), "
                f"{len(subs)} survived"
            )
        elif subs and not vapid_private:
            print(
                "[test-webpush] subscriptions present but VAPID_PRIVATE_KEY "
                "secret is not set — cannot send test push",
                file=sys.stderr,
            )

    # Event log + push fan-out. Collect enriched events first so we can
    # do the event append, ntfy push, and web push in one pass.
    enriched: list[dict] = []
    for source, url, title in new_items:
        category = classify_url(url)
        enriched.append(
            {
                "ts": int(time.time()),
                "source": source.name,
                "category": category,
                "title": title,
                "url": url,
            }
        )
        print(f"NEW [{source.name}/{category}] {title} -> {url}")

    if enriched and not args.dry_run:
        # 1. Append to docs/events.json, newest-first, FIFO cap.
        events = load_events()
        for ev in enriched:
            events.insert(0, ev)
        events = events[:MAX_EVENTS]
        save_events(events)

        # 2. ntfy.sh fan-out (still running in parallel for the transition).
        if topic:
            for ev in enriched:
                try:
                    notify(
                        topic,
                        f"BoC {ev['category']} — new on {ev['source']}",
                        f"{ev['title']}\n{ev['url']}",
                        click_url=ev["url"],
                    )
                except requests.RequestException as exc:
                    print(f"[ntfy] push failed: {exc}", file=sys.stderr)

        # 3. Web Push fan-out to every subscription in docs/subscriptions.json.
        # One tickle push per run — the SW will fetch events.json itself
        # and show the newest entry as a banner. Multiple new items in
        # one run collapse to a single notification that says "tap to see
        # the rest" (user opens the PWA).
        vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
        subs = load_subscriptions()
        if vapid_private and subs:
            subs = send_web_push(subs, vapid_private)
            save_subscriptions(subs)
        elif subs and not vapid_private:
            print(
                "[webpush] VAPID_PRIVATE_KEY not set — skipping web push "
                "even though subscriptions exist",
                file=sys.stderr,
            )

    if not args.dry_run:
        save_state(state)

    print(f"Done. {len(enriched)} new release(s) reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
