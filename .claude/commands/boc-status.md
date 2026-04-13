---
description: Summarize BoC Watcher health from the latest state.json telemetry
allowed-tools: mcp__github__get_file_contents
---

Check the health of the Boards of Canada watcher. Read these files from
GitHub via `mcp__github__get_file_contents`, all on
`refs/heads/claude/ios-album-notifications-akAwd` in `misterizzz/boards-of-canada`:

1. `state.json` — parse the `_telemetry` block
2. `docs/events.json` — count the array length
3. `docs/subscriptions.json` — count the array length

Report concisely:

- **Per source** (Warp, Bleep, Warp Editorial): `matched_count`, `fetch`
  status, `http_status`, and whether `looks_like_cloudflare_challenge` is true
- **Event log**: total events in `docs/events.json` (just the count; don't
  dump them)
- **Subscriptions**: how many push subscriptions are registered
- **Last run**: converted to human-readable delta from now (e.g. "3 min ago",
  "2 hours ago"). Anything older than 15 minutes is suspicious because the
  cron runs every 5 min.
- **Last test push** (if `_telemetry.last_test_push` exists):
  `ntfy_http_status` and whether web push subs survived
  (`last_test_webpush.subscriptions_before` vs `_after`).

**Flag red flags explicitly at the top** of the report before the detail:

- A source with `matched_count: 0` where it was previously populated (known
  healthy baselines: Warp 27, Bleep 25, Warp Editorial 0). A zero on Warp
  Editorial is normal; a zero on Warp or Bleep is a broken selector.
- `fetch != "ok"` on any source.
- Cloudflare challenge detected on any source.
- `last_run` more than 15 minutes in the past.
- Any red flag → start the report with a bold line **"⚠ RED FLAG: <summary>"**.

Keep the report short and scannable — single paragraph summary + bullet list.
No need to dump telemetry verbatim, the user just wants "healthy or not".
