"""Web: search, open URLs, fetch & read pages."""

from __future__ import annotations

import html
import re
import webbrowser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from .registry import ToolError, tool

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=15, http2=False)
    return _client


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(raw)
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


def _ddg_url(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        if "uddg" in q:
            return unquote(q["uddg"][0])
    return href


@tool(risk="low", tags=["open", "website", "url", "link", "browser", "go to"],
      examples=["open_url(url='youtube.com')"])
def open_url(url: str, browser: str = "", profile: str = "") -> str:
    """Open a website (or a web app by name: 'spotify', 'gmail', 'youtube', 'new google doc' = a blank doc ready to
    type in, 'new sheet', 'new slides') in the user's browser. `browser`: chrome | edge | brave (empty = default
    browser). `profile`: which browser profile / account - 'personal', 'school', a profile name or email
    (browser_profiles lists them). This is the user's real, signed-in browser that they see."""
    from .browsers import BROWSERS, open_in, web_app_url

    u = url.strip()
    u = web_app_url(u) or u
    if not re.match(r"^[a-z]+://", u):
        if " " in u or "." not in u:
            u = "https://duckduckgo.com/?q=" + quote_plus(u)
        else:
            u = "https://" + u
    b = browser.strip().lower().replace("google ", "").replace("microsoft ", "")
    if profile and not b:
        b = "chrome"
    if b in BROWSERS:
        return open_in(b, u, profile)
    webbrowser.open(u)
    return f"Opened {u}"


@tool(risk="low", tags=["read", "page", "article", "website", "summarize", "fetch", "url"])
async def fetch_page(url: str, max_chars: int = 10000) -> dict:
    """Download a web page and return its readable text (for reading/summarizing)."""
    u = url if re.match(r"^https?://", url) else "https://" + url
    try:
        r = await client().get(u)
    except httpx.HTTPError as e:
        raise ToolError(f"Couldn't load page: {e}")
    if r.status_code >= 400:
        raise ToolError(f"Page returned HTTP {r.status_code}.")
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        raise ToolError(f"Not a text page ({ctype}).")
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
    text = html_to_text(r.text) if "html" in ctype else r.text
    return {"url": str(r.url), "title": html_to_text(title.group(1)) if title else "",
            "text": text[:max_chars], "truncated": len(text) > max_chars}
