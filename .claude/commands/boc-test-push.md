---
description: Fire a test push through both ntfy and Web Push to verify the pipeline
allowed-tools: Bash, Read, Edit, mcp__github__get_file_contents
---

Trigger an end-to-end test of both push channels.

Steps:

1. `git pull --rebase origin claude/ios-album-notifications-akAwd` to sync.
2. Edit `state.json`: at the top of the JSON object, insert
   `"_request_test_push": true,` as the first key (before `"Bleep"`). Use
   the Edit tool — do not rewrite the whole file. If the key already exists
   (from a previous test that didn't get consumed), do nothing, warn the
   user, and abort.
3. Edit `scraper.py` line 2: replace the existing `# Bump: ...` comment (or
   insert one if missing) with `# Bump: test push <YYYY-MM-DD HH:MM:SS UTC>`
   using the current timestamp. This is what fires the workflow's push
   trigger — `state.json` alone is not in the trigger paths.
4. `python3 -m py_compile scraper.py` as a sanity check.
5. `git add state.json scraper.py`, commit with message
   `Fire manual test push (both channels)`, push to
   `claude/ios-album-notifications-akAwd`.
6. Sleep ~60 seconds (use `run_in_background: true` so the user sees
   progress), then read `state.json` back via `mcp__github__get_file_contents`
   and report:
   - `_telemetry.last_test_push.ntfy_http_status` (should be 200) and `ok`
     (should be `true`).
   - `_telemetry.last_test_webpush.subscriptions_before` vs `_after` — if
     they match, every subscription survived. If `_after` is lower, some
     endpoints were pruned (404/410 from Apple).
   - Confirm `_request_test_push` is no longer in state.json (marker was
     consumed).

If the Action doesn't produce a new bot commit within 2 minutes, tell the
user to check the Actions tab on GitHub — the workflow may have failed or
been skipped.

Keep the report short. User wants "both channels OK" or "channel X failed
because Y".
