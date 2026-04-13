---
description: Append a new Web Push subscription from the PWA to docs/subscriptions.json
argument-hint: '<subscription JSON>'
---

The user has just copied a Web Push subscription JSON from the BoC PWA and
wants it committed so the scraper starts pushing to their new device.

The subscription:

```
$ARGUMENTS
```

Steps:

1. **Validate** the argument is valid JSON with `endpoint`, `keys.p256dh`,
   and `keys.auth` fields. Endpoint should start with `https://`. If any
   field is missing or the JSON is malformed, abort and tell the user what's
   wrong — don't commit garbage to the subscriptions file.
2. `git pull --rebase origin claude/ios-album-notifications-akAwd`.
3. Read `docs/subscriptions.json` (it's a JSON array).
4. If an entry with the same `endpoint` already exists, do nothing and tell
   the user "already registered".
5. If an entry with the same `keys.p256dh` exists under a different
   `endpoint`, this is probably a reinstall on the same device — REPLACE
   that entry rather than appending (the old one is dead, no point keeping
   it). Note this in the commit message.
6. Otherwise append the new subscription to the array.
7. Write the updated array back to `docs/subscriptions.json` (preserve 2-
   space indent, trailing newline).
8. `git add docs/subscriptions.json`, commit with message
   `Register push subscription for <new device | replacing stale entry>`,
   push.
9. Report: total subscription count before vs after, whether this was an
   append or a replace.

Do NOT log or echo the endpoint URL in your response — it ends up in chat
transcripts. Summarize as "registered 1 subscription" without the URL.
