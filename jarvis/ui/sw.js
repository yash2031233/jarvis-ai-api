// Service worker for the installed phone app. Nothing is cached (the page carries a per-launch key and everything
// is live) - it only answers with a clear message when the PC can't be reached instead of a blank screen.
const OFFLINE = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#050302;
color:#f6dcc0;font:16px system-ui;text-align:center;padding:24px;box-sizing:border-box">
<div><p style="font-size:44px;margin:0">◎</p><p>Can't reach Jarvis's PC.</p>
<p style="color:#a07c5c">Is the PC on with Jarvis running, and is Tailscale on (on this phone and the PC)?</p>
<p><button onclick="location.reload()" style="background:#2a160a;color:#ffe2c2;border:1px solid #ffb26b55;
border-radius:10px;padding:10px 18px;font:inherit">Try again</button></p></div>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  const offline = () => new Response(OFFLINE, { headers: { "Content-Type": "text/html" } });
  // Tailscale answers 502 with an empty page when Jarvis isn't running on the PC: show the message instead
  e.respondWith(fetch(e.request).then((r) => (r.status >= 500 ? offline() : r)).catch(offline));
});

// Phone notifications (Web Push): reminders, finished jobs, Jarvis speaking up - even when the app is closed.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Jarvis", {
    body: d.body || "", tag: d.tag || "jarvis", icon: "/icon-192.png", badge: "/icon-192.png", data: { url: d.url || "/" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    const c = cs.find((x) => "focus" in x);
    return c ? c.focus() : self.clients.openWindow((e.notification.data && e.notification.data.url) || "/");
  }));
});
