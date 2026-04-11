// Boards of Canada Watcher — service worker.
// Responsible for (1) shell caching so the app opens offline with the last
// known feed, and (2) Web Push delivery: on `push` we show a notification,
// on `notificationclick` we open the source URL that came in the payload.

const CACHE = "boc-watcher-v1";
const SHELL = [
  "./",
  "index.html",
  "style.css",
  "app.js",
  "manifest.webmanifest",
  "vapid_public.json",
  "icons/icon-192.png",
  "icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Network-first for the event log so the feed stays fresh.
  if (url.pathname.endsWith("/events.json")) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const clone = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, clone));
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }
  // Cache-first for the shell.
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req))
  );
});

// --- Web Push ---------------------------------------------------------
//
// The scraper sends a zero-byte "tickle" push — no payload, just a
// VAPID-signed POST that wakes us up. We then fetch the event log
// ourselves and show the newest entry as a notification. This avoids
// RFC 8291 payload encryption on the sender side.

self.addEventListener("push", (event) => {
  event.waitUntil(
    fetch("events.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      .then((events) => {
        if (!events.length) {
          return self.registration.showNotification("Boards of Canada", {
            body: "Tap to open the watcher.",
            icon: "icons/icon-192.png",
            badge: "icons/icon-192.png",
            data: { url: "./" },
            tag: "boc-empty",
          });
        }
        const latest = events[0];
        const more = events.length > 1 ? ` (+${events.length - 1} more in the app)` : "";
        return self.registration.showNotification(
          `BoC ${latest.category} — new on ${latest.source}`,
          {
            body: `${latest.title}${more}`,
            icon: "icons/icon-192.png",
            badge: "icons/icon-192.png",
            data: { url: latest.url },
            tag: "boc-latest",
            renotify: true,
          }
        );
      })
      .catch((err) => {
        console.error("push handler failed", err);
        return self.registration.showNotification("Boards of Canada", {
          body: "Something new has arrived. Tap to open.",
          icon: "icons/icon-192.png",
          data: { url: "./" },
          tag: "boc-fallback",
        });
      })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        // Prefer reusing an existing window.
        for (const c of clients) {
          if ("focus" in c) {
            c.navigate(target).catch(() => {});
            return c.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
      })
  );
});
