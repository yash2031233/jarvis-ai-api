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
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html" } })));
});
