# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub-Actions-hosted watcher that pushes iOS notifications whenever anything new appears about Boards of Canada on warp.net, bleep.com, or boardsofcanada.com — music releases, merch, videos, editorial articles, site content changes. Two push channels run in parallel:

1. **ntfy.sh** (legacy) — uses the ntfy iOS app
2. **Native Web Push** to a Progressive Web App installed from GitHub Pages (`docs/`), which also serves as a BoC-styled overview feed of every event ever captured.

Everything is driven by one Python script (`scraper.py`), one workflow (`.github/workflows/check.yml`), and a static PWA (`docs/`). No server, no deploy, no app store, no npm build.

## Runtime model

- **Schedule**: `*/5 * * * *` cron in the workflow, plus a `push` trigger scoped to `scraper.py`, `requirements.txt`, and the workflow file itself. Pushing a code change therefore fires the Action within ~20 seconds. `state.json`, `docs/events.json`, `docs/subscriptions.json` and `docs/icons/**` are deliberately NOT in the push paths so the bot's own commits cannot recursively retrigger.
- **Concurrency**: `group: boc-watcher`, `cancel-in-progress: true`, `timeout-minutes: 5`. Don't flip `cancel-in-progress` to false — a hung run (e.g. a broken `pip install`) will silently queue every subsequent push behind it if you do. The 5-minute job timeout caps the blast radius of bugs.
- **State**: `state.json` in the repo root (current snapshot per source). The workflow's final step commits it back with a `github-actions[bot]` author whenever it changed. State writes go through an `_atomic_write_text()` helper (tmp-file + `os.replace`) so `cancel-in-progress` cannot leave a half-written JSON behind. `load_state()` falls back to `{}` on `JSONDecodeError`, consistent with `load_events()` / `load_subscriptions()`.
- **Default branch**: `main` — that's where cron fires and where bot commits land. The workflow dual-pushes every bot commit to both `main` AND `claude/ios-album-notifications-akAwd` (which GitHub Pages serves the PWA from). The feature branch push uses `--force-with-lease` after an explicit `git fetch` of that ref (without the fetch, Actions' shallow checkout doesn't know the feature branch tip and refuses the push — this bug cost us hours; see commit fa7f3ad). Before the push to `main`, the commit step does `git fetch origin main && git rebase -X theirs origin/main` so a workflow triggered on the feature branch doesn't get rejected non-fast-forward when main moved ahead via a separate cron run; this requires `fetch-depth: 0` on the checkout step.
- **Secrets**: `NTFY_TOPIC` (ntfy.sh topic name, optional), `VAPID_PRIVATE_KEY` (base64url-encoded raw 32-byte ECDSA P-256 scalar, used by `_build_vapid_auth_header()` to sign Web Push requests to Apple).

## Web Push is self-implemented, NOT pywebpush

The first attempt shipped `pywebpush` as a dependency but its transitive dep `http-ece` needs Rust/C build tools and failed on the Ubuntu runner. We pivoted to a self-contained implementation:

- `_build_vapid_auth_header()` in `scraper.py` builds an RFC 8292 Authorization header: ECDSA P-256 sign with the raw 32-byte private scalar from `VAPID_PRIVATE_KEY`, JWT with `aud`/`exp`/`sub` claims. Only depends on `cryptography` (has manylinux wheels, installs instantly). The function asserts the decoded scalar is exactly 32 bytes — a hex-encoded or otherwise malformed `VAPID_PRIVATE_KEY` raises a clear `ValueError` instead of an opaque cryptography stack trace.
- `send_web_push()` POSTs an **empty-body "tickle" push** — no payload encryption, just the Authorization header. The service worker's `push` handler fetches `docs/events.json` itself and shows the newest entry as a notification. One extra HTTP round trip per push, zero compiled deps.
- One push per run regardless of how many items are new — the banner shows the top event, the feed shows the rest. Collapsing avoids needing RFC 8291 aes128gcm payload encryption.

## PWA + GitHub Pages

`docs/` is a static PWA served via GitHub Pages from the default branch, `/docs` folder. Pure vanilla JS/CSS/HTML.

1. **Overview feed** — `app.js` reads `docs/events.json` (append-only log, newest first, FIFO capped at 500) and renders a BoC-styled list (cream + burnt orange + mossy green + coffee). Polls every 5 minutes.
2. **Web Push client** — tap "Enable notifications" → SW subscribes via the browser's push service using the VAPID public key in `docs/vapid_public.json`. The resulting subscription JSON is shown in-app with a "Copy to clipboard" button; the user pastes it in chat and an agent commits it to `docs/subscriptions.json` (the only manual step per device). On subsequent opens `app.js` compares the local `pushSubscription.endpoint` against `docs/subscriptions.json`; if it matches, the UI goes quiet ("Notifications enabled ✓").
3. **Cache busting** — `sw.js` has a `CACHE = "boc-watcher-vN"` constant. Bump `N` whenever `app.js` / `sw.js` / `index.html` change so existing installs pick up the new shell instead of serving stale cache-first copies.

### Icon bootstrap

`docs/icons/icon-{192,512}.png` are PIL-drawn hexagon flower patterns (7 flat-top white hexagons on pure black, matching boardsofcanada.com's `#000000` background). Generated locally and committed directly — no runtime bootstrap step in the workflow. `docs/icons/.source` is `hexagons-v1`.

## Event log (`docs/events.json`) vs state snapshot (`state.json`)

`state.json` is a snapshot of the currently-visible URLs per source, overwritten every run. `docs/events.json` is an append-only list of every new URL ever detected, `{ts, source, category, title, url}` per event. Both are written in one `main()` pass in `scraper.py`:

1. Source extraction builds `new_items` (only on non-first runs where something actually changed).
2. For every new item we `classify_url()` it into `music`/`merch`/`news`/`video`/`tour`/`update` and prepend to events.
3. Fan-out: ntfy (if `NTFY_TOPIC` is set) and Web Push (if `VAPID_PRIVATE_KEY` is set AND `docs/subscriptions.json` has entries). One ntfy push per item, ONE web push tickle per run.

Dead Web Push subscriptions (404/410 from Apple) are pruned by `send_web_push()` and written back to `docs/subscriptions.json` on the same commit.

## Source model

Everything revolves around the `Source` dataclass in `scraper.py`. Each source owns one URL and a detection strategy. Three strategies exist:

### 1. Anchor-based extraction (default)

Filters applied to every `<a>` anchor on the fetched HTML:

- `path_markers: tuple[str, ...]` — a URL matches if its path contains ANY of these.
- `required_slug: str | None` — URL path must additionally contain this string. Used on Bleep to require `"boards-of-canada"` because Bleep's `/release/` URLs list music and merch for many artists.
- `required_text: str | None` — anchor's visible text must contain this substring (case-insensitive). Used by `Warp Editorial` because editorial URL slugs don't embed artist names, only the article title does.
- `title_from_slug: bool` — force title derivation from the URL slug instead of `anchor.get_text()`. Needed for Bleep where the useful text is behind an `<img>` and the outer anchor text is a format selector like `"LP Download"` or `"LP CD Download"`.
- `canonicalize: Callable[[str], str]` — applied after filtering to collapse sub-pages. Only `_warp_canonical()` is used, stripping `/tracklist`, `/reviews`, `/credits` off `warp.net/releases/<slug>/*`.

Same-netloc filter: `extract_releases()` drops any anchor whose absolute URL has a different netloc than the source URL. This prevents broad `path_markers` like `("/",)` from sweeping in external links (twitter, youtube, etc.).

### 2. Text-content hash (`detect_via_content_hash: bool`)

For pages with zero `<a>` tags (JS-rendered SPAs, splash pages). Strips `<script>`, `<style>`, `<meta>`, `<link>`, lowercases, collapses whitespace, SHA256-hashes the visible text. Any hash flip = one push. Used by `BoC Official` (boardsofcanada.com) and `Bandcamp` (which serves a JS anti-bot challenge page from GitHub Actions IPs).

### 3. Raw-bytes hash (`raw_bytes_hash: bool`)

For non-HTML sources: JS bundles, images, JSON APIs. SHA256-hashes `resp.content` directly. Any byte-level change = one push. Used by `BoC Klaviyo` (email-campaign JS), `BoC Hero Image` (bg.jpg), `BoC Sharing Image` (og:image).

### Source-specific headers

`user_agent_override: str | None` — per-source User-Agent for APIs that reject generic browser UAs. Not currently used (MusicBrainz and Discogs were removed) but the mechanism is in place if a future API source needs it.

### URL classification

`classify_url()` runs at notify time and labels each new URL. `MERCH_SLUG_SUBSTRINGS` is the hardcoded list (`t-shirt`, `hoodie`, `sweatshirt`, `tote`, `mug`, etc.) used to distinguish merch from music inside Bleep's `/release/` URL space. Warp keeps merch under `/products/` so doesn't need this; add to the list if a new merch type appears on Bleep.

## Feedback-loop debugging pattern

The sandbox that built this project has no outbound HTTP (WebFetch 403s everything, even example.com). All knowledge of what Warp and Bleep actually serve has to come from the Action runs themselves:

1. Edit `scraper.py`, push.
2. Push trigger fires the Action within ~20s.
3. Action scrapes, writes `state.json` with a `_telemetry` block, commits back.
4. Read `state.json` via `mcp__github__get_file_contents` (the file in git is truth; local working copy lags behind bot commits).
5. `_telemetry.sources[*].path_prefix_inventory` is the key diagnostic: every anchor on the fetched page bucketed by its first two path segments with count + one example per bucket. Reveals what URL spaces exist without needing to read raw HTML.
6. Adjust `path_markers` / `required_slug` / `required_text`, push again.

When local `git push` is rejected for non-fast-forward, the bot committed state / events while you weren't looking — `git pull --rebase` resolves it (drop any `state.json` / `docs/events.json` rebase conflict in favour of the incoming bot version, continue). You cannot put these paths in the push trigger to avoid the conflicts — that would cause an infinite workflow loop.

## Test-push mechanism

There is no MCP tool that triggers `workflow_dispatch`, so verification of the push pipeline happens via a one-shot state marker:

1. Set `state["_request_test_push"] = true` in `state.json` and push **alongside a code change** (state.json alone doesn't match the push trigger paths, so a scraper/workflow edit is required to fire the Action).
2. Next run pops the marker, and:
   - Sends an ntfy test push, records `_telemetry.last_test_push.{ntfy_http_status, ok, sent_at}` (status `200` + `ok: true` is success).
   - Sends a Web Push tickle to every subscription, records `_telemetry.last_test_webpush.{sent_at, subscriptions_before, subscriptions_after}`. `before == after` means every subscription survived (2xx from Apple); `after < before` means at least one endpoint was 410/404 and got pruned.

**Never store the ntfy response body** anywhere — ntfy echoes the topic name back in its JSON and `state.json` is in a public repo. See commits 90ac786 / 4f97413 for the one time this was messed up; only the status code + a boolean is safe.

To verify Web Push without the marker you need a live subscription in `docs/subscriptions.json` first (the user subscribes from the PWA, pastes the JSON in chat, an agent commits it).

## First-run vs transient-blip semantics

Two distinct cases produce a 0-result extraction, handled differently:

- **First run for a source** (name not in `state`): write an empty baseline. If you don't do this, a source that correctly returns 0 results today — e.g. Warp Editorial when no BoC article is on page 1 — stays in "first-run" mode forever and silently absorbs the eventual first real match as "baseline". Bug introduced and fixed once; see commit 112cc43.
- **Subsequent run returning 0 results**: keep the stored state untouched. Assume transient fetch blip or broken selector — don't clobber a working baseline and don't spam notifications on recovery.

## Slash commands and session auto-check

`.claude/commands/` ships three project-local slash commands:

- **`/boc-status`** — reads `state.json` + `docs/events.json` + `docs/subscriptions.json` via MCP and summarises telemetry, flagging red flags (matched_count=0 on a previously-populated source, Cloudflare challenge, stale `last_run`). Pre-approved for `mcp__github__get_file_contents` so it doesn't prompt per invocation.
- **`/boc-test-push`** — sets `state["_request_test_push"] = true`, bumps the timestamp comment on `scraper.py:2`, commits and pushes. Waits for the Action and reports both `last_test_push.ntfy_http_status` and `last_test_webpush.subscriptions_{before,after}`.
- **`/boc-add-sub <JSON>`** — validates a pushSubscription JSON, appends (or replaces on matching p256dh) into `docs/subscriptions.json`, commits and pushes. Never logs the endpoint URL back in chat — it's sensitive.

`.claude/settings.json` registers a SessionStart hook (matcher `startup`) that echoes `/boc-status` on every fresh Claude Code session opened in this repo. Effect: each session auto-begins with a health check of the watcher. Comment out the hook if that becomes noise.

## Running locally

```bash
pip install -r requirements.txt
NTFY_TOPIC=dummy python3 scraper.py --dry-run   # no push, no state write
python3 -m py_compile scraper.py                 # syntax check
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/check.yml'))"  # workflow YAML check
```

No tests exist. The feedback loop IS the test.

## Adding a new source

Append a `Source(...)` entry to `SOURCES` in `scraper.py`. On the first run you typically don't know what path markers the page uses — look at `_telemetry.sources[<name>].path_prefix_inventory` after pushing, pick the right prefix(es), push again. Don't trim the telemetry until the selector is confirmed working.

If a new source shares a URL with an existing one the scraper fetches twice — acceptable, each fetch takes ~1s and total run stays well under the 5-minute timeout.

## Gotchas (each one has cost us a round-trip at least once)

- **YAML block scalars vs bash heredocs.** In a `run: |` block, every line of a bash heredoc `<<PY ... PY` must be indented at the same level as the surrounding YAML. A Python heredoc body at column 0 breaks out of the YAML block scalar and makes the whole file unparseable — GitHub silently refuses to run the workflow with no visible error. If you need multi-line Python in a step, use `python3 -c "..."` with the Python inside a bash double-quoted string; YAML then strips the common indent uniformly and Python receives code at column 0. See commit 323c6d5 for the 5-commit saga where every push was silently dropped.
- **GitHub scheduled cron can be delayed 10+ minutes** under load. The `push` trigger is the only way to get sub-minute feedback during selector work. `cancel-in-progress: true` is essential — without it, one stuck run queues everything behind it indefinitely.
- **Workflow registration needs the file on the default branch.** When the repo was created, `main` didn't exist and the first push made `claude/ios-album-notifications-akAwd` the default — workflows only run from there. Switching the default via Settings → Branches is a manual user step.
- **ntfy.sh topics are publicly guessable**, not private channels. Anyone who knows the topic name can publish to it and read its message stream. Never log or commit the topic value.
- **Bleep artist IDs are not slug-validated.** `https://bleep.com/artist/48-boards-of-canada` 301-redirects to a completely different artist (A Guy Called Gerald). Always cross-check the artist ID against `final_url` in the Bleep telemetry; the correct BoC ID is 78.
- **Never add `br` (brotli) to Accept-Encoding.** Python's `requests` library on Ubuntu runners has no brotli decoder by default. If you advertise `br`, servers that negotiate brotli return bytes we can't decode — BeautifulSoup parses garbage, hashes flip randomly per run, and every source fires false-positive pushes. Stick to `gzip, deflate` only. See the multi-commit brotli saga in the history for the full horror.
- **boardsofcanada.com is a JS-rendered SPA** — the initial HTML is a 6.9 KB shell with zero `<a>` tags. The visible content (Klaviyo email form) is injected client-side. Anchor-based extraction finds nothing; use `detect_via_content_hash` instead. The Klaviyo JS bundle (`static.klaviyo.com/onsite/js/Rwheqg/klaviyo.js`) and the hero/sharing images are monitored separately via `raw_bytes_hash` to catch campaign updates.
- **Bandcamp serves a JS challenge page** ("Client Challenge") to GitHub Actions IPs. No headers or User-Agent tweaking bypasses it. Source is in `detect_via_content_hash` mode as a best-effort signal — at least it detects if the challenge page itself changes.
- **fetch() returns bytes, not str.** Callers that need HTML text decode with `content.decode("utf-8", errors="replace")`. Hash-based sources use the raw bytes directly.
- **iOS 18 sometimes classifies PWAs as bookmarks on install.** If the user reports the PWA only lives in App Library (not the home screen) and the long-press menu shows only "Delete bookmark" / "Share bookmark" with no "Add to Home Screen" option, they need to: `Settings → Home Screen & App Library → Newly Downloaded Apps → "Add to Home Screen"`, then purge old entries (Safari bookmarks + website data for `misterizzz.github.io`), reboot, and reinstall via Safari. Even then it sometimes stays in App Library only — the functionality is unaffected (Spotlight opens it, push still delivers), it's purely an Apple quirk.
- **Every PWA uninstall/reinstall generates a fresh `pushSubscription`** with a new endpoint. Commit the new one in `docs/subscriptions.json`; the stale one gets pruned automatically on its next push attempt when Apple returns 410. Don't try to "edit" an existing subscription in place.
- **Dual-push to `main` is rejected non-fast-forward when the feature branch lags.** The commit step's `git push origin HEAD:refs/heads/main` only succeeds if HEAD is a fast-forward of `origin/main`. When the workflow is triggered on the feature branch and main moved ahead via a separate cron run on main, the push is rejected with `! [rejected] HEAD -> main (fetch first)` and the entire run fails (no state update, no notifications fire — even the test-push marker is not consumed). Mitigation: the commit step does `git fetch origin main && git rebase -X theirs origin/main` first, with `fetch-depth: 0` on the checkout so rebase can find a common ancestor. `-X theirs` in rebase context = "prefer the commit being rebased" (= our just-scraped state). Real symptom seen on Action run #160 (2026-04-16); fixed in commit d061c6a.
