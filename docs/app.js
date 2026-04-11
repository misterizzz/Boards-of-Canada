// Boards of Canada Watcher — client-side feed + push subscription.
//
// Reads docs/events.json for the event log, renders a BoC-styled feed,
// polls for updates every 5 minutes, and handles the Web Push subscription
// flow (tap "Enable notifications" → subscribes via the service worker →
// shows the subscription JSON for the user to commit to subscriptions.json).

const VAPID_URL = "vapid_public.json";
const EVENTS_URL = "events.json";
const POLL_MS = 5 * 60 * 1000;

// --------------------------------------------------------------------- feed

async function loadEvents() {
  try {
    const r = await fetch(EVENTS_URL, { cache: "no-store" });
    if (!r.ok) return [];
    return await r.json();
  } catch (e) {
    console.warn("failed to load events.json", e);
    return [];
  }
}

function relativeTime(ts) {
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

function render(events) {
  const feed = document.getElementById("feed");
  const meta = document.getElementById("feed-meta");
  if (!events.length) {
    feed.innerHTML = "";
    meta.textContent =
      "No events yet. Baseline has been captured; the next real drop will appear here.";
    return;
  }
  meta.textContent = `${events.length} event${events.length === 1 ? "" : "s"} tracked.`;
  feed.innerHTML = events
    .map(
      (e) => `
    <article class="event cat-${escapeHtml(e.category || "update")}">
      <header>
        <span class="badge">${escapeHtml(e.category || "update")}</span>
        <time datetime="${new Date(e.ts * 1000).toISOString()}">${relativeTime(e.ts)}</time>
      </header>
      <h2><a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.title)}</a></h2>
      <footer>${escapeHtml(e.source)}</footer>
    </article>
  `
    )
    .join("");
}

async function refresh() {
  const events = await loadEvents();
  render(events);
}

refresh();
setInterval(refresh, POLL_MS);

// --------------------------------------------------------------------- push

const pushButton = document.getElementById("push-button");
const pushStatus = document.getElementById("push-status");
const pushShare = document.getElementById("push-share");
const pushJson = document.getElementById("push-json");
const pushCopy = document.getElementById("push-copy");

function urlBase64ToUint8Array(base64String) {
  // Convert URL-safe base64 (no padding) to a Uint8Array the Web Push API
  // wants for applicationServerKey.
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function supportsPush() {
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function getExistingSubscription() {
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function subscribeForPush() {
  pushStatus.textContent = "Asking permission…";
  const perm = await Notification.requestPermission();
  if (perm !== "granted") {
    pushStatus.textContent =
      "Permission denied. Open iOS Settings → Notifications → BoC to allow.";
    return;
  }

  pushStatus.textContent = "Fetching VAPID key…";
  const vapidResp = await fetch(VAPID_URL, { cache: "no-store" });
  const { key: vapidPublic } = await vapidResp.json();

  pushStatus.textContent = "Registering with Apple Push…";
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(vapidPublic),
  });

  showSubscription(sub, true);
  pushStatus.textContent =
    "Subscribed. Copy the JSON below and paste it in the chat — I'll commit it.";
  pushButton.hidden = true;
}

function showSubscription(sub, openPanel = true) {
  const json = JSON.stringify(sub.toJSON(), null, 2);
  pushJson.value = json;
  pushShare.hidden = false;
  pushShare.open = openPanel;
}

async function isRegisteredOnServer(subscription) {
  try {
    const r = await fetch("subscriptions.json", { cache: "no-store" });
    if (!r.ok) return false;
    const list = await r.json();
    const needle = subscription.endpoint;
    return Array.isArray(list) && list.some((s) => s && s.endpoint === needle);
  } catch {
    return false;
  }
}

function markEnabled() {
  pushStatus.textContent = "Notifications enabled on this device.";
  pushButton.textContent = "Notifications enabled ✓";
  pushButton.hidden = true;
  pushShare.hidden = true;
}

async function initPush() {
  if (!supportsPush()) {
    pushStatus.textContent =
      "This browser does not support Web Push. Install the PWA to the Home Screen on iOS 16.4+ to enable notifications.";
    return;
  }
  pushButton.hidden = false;
  try {
    const existing = await getExistingSubscription();
    if (existing) {
      const registered = await isRegisteredOnServer(existing);
      if (registered) {
        markEnabled();
      } else {
        showSubscription(existing, true);
        pushButton.hidden = true;
        pushStatus.textContent =
          "Subscribed on this device but not yet registered on the server. Copy the JSON and paste it in the chat.";
      }
    } else {
      pushStatus.textContent = "Not subscribed yet on this device.";
    }
  } catch (e) {
    console.warn(e);
  }
  pushButton.addEventListener("click", () => {
    subscribeForPush().catch((e) => {
      console.error(e);
      pushStatus.textContent = `Subscribe failed: ${e.message}`;
    });
  });
  pushCopy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pushJson.value);
      pushCopy.textContent = "Copied!";
      setTimeout(() => (pushCopy.textContent = "Copy to clipboard"), 1800);
    } catch {
      pushJson.select();
      document.execCommand("copy");
    }
  });
}

initPush();
