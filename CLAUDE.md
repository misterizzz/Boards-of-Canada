# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub-Actions-hosted watcher that pushes iOS notifications whenever anything new appears about Boards of Canada on warp.net or bleep.com — music releases, merch, videos, editorial articles. Two push channels run in parallel:

1. **ntfy.sh** (legacy) — uses the ntfy iOS app
2. **Native Web Push** to a Progressive Web App installed from GitHub Pages (`docs/`), which also serves as a BoC-styled overview feed of every event ever captured.

Everything is driven by one Python script (`scraper.py`), one workflow (`.github/workflows/check.yml`), and a static PWA (`docs/`). No server, no deploy, no app store.

## Runtime model

- **Schedule**: `*/5 * * * *` cron in the workflow, plus a `push` trigger scoped to `scraper.py`, `requirements.txt`, and the workflow file itself. Pushing a code change therefore fires the Action within ~20 seconds — useful for fast iteration. `state.json`, `docs/events.json`, `docs/subscriptions.json` and `docs/icons/**` are deliberately NOT in the push paths so the bot's own commits cannot recursively retrigger.
- **State**: `state.json` in the repo root (current snapshot per source). The workflow's final step commits it back to the branch with a `github-actions[bot]` author whenever it changed.
- **Default branch**: `claude/ios-album-notifications-akAwd` — that's where cron fires and where bot commits land. `main` exists but has diverged; don't fast-forward it without an explicit user OK (would need a force-push).
- **Secrets**: `NTFY_TOPIC` (ntfy.sh topic name, optional), `VAPID_PRIVATE_KEY` (base64url-encoded raw 32-byte ECDSA P-256 scalar, used by `pywebpush` to sign push requests to Apple).

## PWA + GitHub Pages

`docs/` is a static PWA served via GitHub Pages from the default branch, `/docs` folder. It does two things:

1. **Overview feed** — reads `docs/events.json` (append-only log of every detection, newest first, FIFO capped at 500) and renders a BoC-styled list. Poll interval: 5 minutes.
2. **Web Push client** — on tap of "Enable notifications", service worker subscribes to the browser's push service using the VAPID public key in `docs/vapid_public.json`. The resulting subscription JSON is shown in-app for the user to copy; a human then commits it to `docs/subscriptions.json` (this is the only manual step per device).

The PWA is pure vanilla JS/CSS/HTML — no build step, no framework. `docs/icons/icon-512.png` is bootstrapped by the workflow on first run (a placeholder 1×1 PNG gets replaced by the Deezer artist photo via `curl` + Pillow).

## Event log (`docs/events.json`) vs state snapshot (`state.json`)

`state.json` is a snapshot of the currently-visible URLs per source, overwritten every run. `docs/events.json` is an append-only list of every new URL ever detected, with `{ts, source, category, title, url}` per event. They're written in the same `main()` pass in `scraper.py`:

1. Source extraction builds the `new_items` list (only on non-first runs where something actually changed).
2. For every new item we `classify_url()` it into music/merch/news/video/tour/update and append to events.
3. We fan out to both ntfy (if `NTFY_TOPIC` is set) and Web Push (if `VAPID_PRIVATE_KEY` is set AND `docs/subscriptions.json` has entries).

Dead Web Push subscriptions (404/410 from Apple) are pruned automatically by `send_web_push()` and written back to `docs/subscriptions.json` on the same commit.

## Feedback-loop debugging pattern

The sandbox this project was built in has no outbound HTTP (WebFetch 403s everything, including example.com). All knowledge of what Warp and Bleep actually serve has to come from the Action runs themselves. The standard iteration loop is:

1. Edit `scraper.py`, push.
2. Push trigger fires the Action within ~20s.
3. Action scrapes, writes `state.json` with a `_telemetry` block, commits back.
4. Read `state.json` via `mcp__github__get_file_contents` (the file in git is truth; local working copy lags behind bot commits).
5. The `_telemetry.sources[*].path_prefix_inventory` is the key diagnostic: it buckets every anchor on the fetched page by its first two path segments with counts and one example per bucket, so you can see what URL spaces actually exist without needing to read the raw HTML.
6. Adjust `path_markers` / `required_slug` / `required_text`, push again.

When local `git push` is rejected for non-fast-forward, the cause is almost always that the bot committed an updated `state.json` or `docs/events.json` in between — `git pull --rebase` resolves it (drop any conflict in favor of the incoming bot version, then continue). You cannot avoid these conflicts by including `state.json` or `docs/events.json` in the push trigger paths — that would cause an infinite workflow loop.

## Feedback-loop debugging pattern

The sandbox this project was built in has no outbound HTTP (WebFetch 403s everything, including example.com). All knowledge of what Warp and Bleep actually serve has to come from the Action runs themselves. The standard iteration loop is:

1. Edit `scraper.py`, push.
2. Push trigger fires the Action within ~20s.
3. Action scrapes, writes `state.json` with a `_telemetry` block, commits back.
4. Read `state.json` via `mcp__github__get_file_contents` (the file in git is truth; local working copy lags behind bot commits).
5. The `_telemetry.sources[*].path_prefix_inventory` is the key diagnostic: it buckets every anchor on the fetched page by its first two path segments with counts and one example per bucket, so you can see what URL spaces actually exist without needing to read the raw HTML.
6. Adjust `path_markers` / `required_slug` / `required_text`, push again.

When local `git push` is rejected for non-fast-forward, the cause is almost always that the bot committed an updated `state.json` in between — `git pull --rebase` resolves it (drop any `state.json` rebase conflict in favor of the incoming bot version, then continue). You cannot avoid these conflicts by including `state.json` in the push trigger paths — that would cause an infinite workflow loop.

## Test-push mechanism

There is no MCP tool that triggers `workflow_dispatch`, so to verify the full ntfy pipeline end-to-end you use a one-shot state marker:

1. Set `state["_request_test_push"] = true` in `state.json` and push along with any code change (code change is needed because `state.json` alone doesn't trigger the Action).
2. Next Action run pops the marker, calls `notify()`, and records `_telemetry.last_test_push` with `ntfy_http_status` and an `ok` boolean. **Do not** store the ntfy response body anywhere — ntfy echoes the topic name back and `state.json` is in a public repo. See commit 90ac786 / 4f97413 in the history for the one time this was messed up.

To verify Web Push end-to-end you need a live subscription in `docs/subscriptions.json` first, then either wait for a real detection or synthetically inject one by temporarily removing a URL from `state.json` so the next run sees it as "new". The scraper will then send the test push to every subscription via `pywebpush` and log the outcome.

## Source model

Everything revolves around the `Source` dataclass in `scraper.py`. Each source owns one URL and a set of filters applied to every `<a>` anchor on the fetched HTML:

- `path_markers: tuple[str, ...]` — a URL matches if its path contains ANY of these.
- `required_slug: str | None` — if set, the URL path must contain this string (used on Bleep to require `"boards-of-canada"` because Bleep's `/release/` URLs list music and merch for many artists).
- `required_text: str | None` — if set, the anchor's visible text must contain this substring (case-insensitive). Used by `Warp Editorial` because editorial URL slugs don't embed artist names, only the article title does.
- `title_from_slug: bool` — force title derivation from the URL slug instead of `anchor.get_text()`. Needed for Bleep where the useful text is behind an `<img>` and the outer anchor text is a format selector like `"LP Download"` or `"LP CD Download"`.
- `canonicalize: Callable[[str], str]` — applied after filtering to collapse sub-pages into a single release URL. Currently only `_warp_canonical()` is used, which strips `/tracklist`, `/reviews`, `/credits` etc. off `warp.net/releases/<slug>/*`.

`classify_url()` runs at notify time and labels each new URL with `music` / `merch` / `news` / `video` / `tour` / `update` based on path prefixes and merch keyword scanning. The label goes into the ntfy push title (`"BoC merch — new on Bleep"`, etc.).

## First-run vs transient-blip semantics

Two distinct cases produce a 0-result extraction, handled differently:

- **First run for a source** (name not in state): write an empty baseline. Future runs can then diff against it. If you don't do this, a source that is correctly returning 0 results today — e.g. Warp Editorial when no BoC article is on page 1 — stays in "first-run" mode forever and silently absorbs the eventual first real match. Bug was introduced and fixed once; see commit 112cc43.
- **Subsequent run returning 0 results**: keep the stored state untouched. Assume it's a transient fetch blip or a selector that broke — do not clobber a working baseline and do not spam notifications on recovery.

## The MERCH_SLUG_SUBSTRINGS list

`scraper.py` hardcodes a tuple of substrings (`t-shirt`, `hoodie`, `sweatshirt`, `tote`, `mug`, etc.) used only by `classify_url()` to distinguish merch from music inside Bleep's `/release/` URL space. Warp keeps merch under `/products/` so it doesn't need this. When Warp or Bleep introduces a new merch line (posters, slipmats, whatever), add its slug substring here so the category label stays correct.

## Running locally

```bash
pip install -r requirements.txt
NTFY_TOPIC=dummy python3 scraper.py --dry-run   # no push, no state write
python3 -m py_compile scraper.py                 # syntax check
```

No tests exist. The feedback loop IS the test — push the change, read `state.json` back from the remote, verify the diff.

## Adding a new source

Append a `Source(...)` entry to `SOURCES` in `scraper.py`. For the first run you'll want to look at its `_telemetry.path_prefix_inventory` after pushing, because you usually won't know upfront which path markers the page uses. The `_telemetry` block is large and verbose on purpose — don't slim it down until new selectors are confirmed working.

If the new source shares a URL with an existing one, it'll fetch twice (no HTML cache). That's currently acceptable — 4 sources × 30s per run is well under the Actions budget.

## Gotchas

- **GitHub scheduled cron can be delayed 10+ minutes** under load. The `push` trigger is much more responsive and is the only way to get sub-minute feedback during selector work.
- **Workflow registration needs the file on the default branch.** When the repo was freshly created, `main` didn't exist and the first push made `claude/ios-album-notifications-akAwd` the default — workflows only run from there. Switching the default via Settings → Branches would require manual user action.
- **ntfy.sh topics are publicly guessable**, not private channels. Anyone who knows the topic name can publish to it. Never log or commit the topic value; see the test-push section above.
- **Bleep artist IDs are not slug-validated.** `https://bleep.com/artist/48-boards-of-canada` 301-redirects to a completely different artist (A Guy Called Gerald). Always cross-check the artist ID against `final_url` in the Bleep telemetry; the correct BoC ID is 78.
