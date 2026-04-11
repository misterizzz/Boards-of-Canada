# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub-Actions-hosted watcher that pushes iOS notifications via ntfy.sh whenever anything new appears about Boards of Canada on warp.net or bleep.com — new music releases, new merch, new videos, new editorial articles. Everything is driven by one Python script (`scraper.py`) and one workflow (`.github/workflows/check.yml`). There is no server, no deploy, no app.

## Runtime model

- **Schedule**: `*/5 * * * *` cron in the workflow, plus a `push` trigger scoped to `scraper.py`, `requirements.txt`, and the workflow file itself. Pushing a code change therefore fires the Action within ~20 seconds — useful for fast iteration. `state.json` is deliberately NOT in the push paths so the bot's own state commits cannot recursively retrigger.
- **State**: `state.json` in the repo root. The workflow's final step commits it back to the branch with a `github-actions[bot]` author whenever it changed.
- **Default branch**: `claude/ios-album-notifications-akAwd` — that's where cron fires and where bot state commits land. `main` exists but has diverged; don't fast-forward it without an explicit user OK (would need a force-push).
- **Secret**: `NTFY_TOPIC` in repo Actions secrets. It is the ntfy.sh topic name (no URL, no prefix).

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
