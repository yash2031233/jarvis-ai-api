"""Web search that keeps working, and actually reads the results.

Sources, first that answers wins (free ones can block automated searches for a while, so there's always another):
  Brave Search API  with a key in Settings (keychain: brave_search_key) - the most reliable, free tier available
  SearXNG           your own instance, `searxng_url` setting
  Bing              its RSS feed (no key)
  DuckDuckGo        HTML, then Lite (no key)
With read > 0 the top results are opened in parallel and their main text comes back with them, so the model answers
from the pages themselves instead of guessing from two-line snippets. kind="news" searches recent news.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

import httpx

from .. import config
from .registry import ToolError, tool
from .web import _ddg_url, client, html_to_text

log = logging.getLogger(__name__)
_blocked: dict[str, float] = {}             # source -> until when to skip it (it refused us recently)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(html.unescape(s or ""))).strip()


async def _brave(q: str, n: int, news: bool) -> list[dict]:
    key = config.get_secret("brave_search_key")
    if not key:
        return []
    url = "https://api.search.brave.com/res/v1/" + ("news/search" if news else "web/search")
    r = await client().get(url, params={"q": q, "count": n}, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    j = r.json()
    items = j.get("results") if news else (j.get("web") or {}).get("results", [])
    return [{"title": _clean(x.get("title", "")), "url": x.get("url", ""), "snippet": _clean(x.get("description", "")),
             "date": x.get("age") or x.get("page_age") or ""} for x in items or []]


async def _searx(q: str, n: int, news: bool) -> list[dict]:
    base = (config.store.load().searxng_url or "").rstrip("/")
    if not base:
        return []
    r = await client().get(f"{base}/search", params={"q": q, "format": "json", "categories": "news" if news else "general"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": _clean(x.get("content", "")),
             "date": x.get("publishedDate") or ""} for x in r.json().get("results", [])[:n]]


async def _bing(q: str, n: int, news: bool) -> list[dict]:
    url = "https://www.bing.com/news/search" if news else "https://www.bing.com/search"
    r = await client().get(url, params={"q": q, "format": "rss", "setlang": "en"})
    r.raise_for_status()
    out = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", r.text):
        def tag(t: str) -> str:
            m = re.search(rf"(?s)<{t}>(.*?)</{t}>", item)
            return html.unescape(m.group(1)) if m else ""
        link = tag("link")
        if "bing.com/news/apiclick" in link:            # news links go through a redirect: take the real one
            m = re.search(r"url=([^&]+)", link)
            if m:
                from urllib.parse import unquote

                link = unquote(m.group(1))
        out.append({"title": _clean(tag("title")), "url": link, "snippet": _clean(tag("description")),
                    "date": tag("pubDate")})
    return out[:n]


async def _ddg(q: str, n: int, news: bool) -> list[dict]:
    r = await client().post("https://html.duckduckgo.com/html/", data={"q": q})
    r.raise_for_status()
    out = []
    for href, title, snippet in re.findall(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                                           r'(?:class="result__snippet"[^>]*>(.*?)</a>)?', r.text):
        u = _ddg_url(html.unescape(href))
        if "duckduckgo.com/y.js" not in u:
            out.append({"title": _clean(title), "url": u, "snippet": _clean(snippet or "")})
    return out[:n]


async def _ddg_lite(q: str, n: int, news: bool) -> list[dict]:
    r = await client().get("https://lite.duckduckgo.com/lite/", params={"q": q})
    r.raise_for_status()
    links = re.findall(r"(?s)<a[^>]+class=.result-link.[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r.text)
    snips = re.findall(r"(?s)class=.result-snippet.[^>]*>(.*?)</td>", r.text)
    return [{"title": _clean(t), "url": _ddg_url(html.unescape(h)), "snippet": _clean(snips[i] if i < len(snips) else "")}
            for i, (h, t) in enumerate(links)][:n]


SOURCES = [("brave", _brave), ("searxng", _searx), ("bing", _bing), ("duckduckgo", _ddg), ("duckduckgo-lite", _ddg_lite)]


def _main_text(raw_html: str) -> str:
    """The readable part of a page: <article>/<main> when there is one, without menus, footers and one-word lines."""
    m = re.search(r"(?is)<article[^>]*>(.*)</article>", raw_html) or re.search(r"(?is)<main[^>]*>(.*)</main>", raw_html)
    txt = html_to_text(m.group(1) if m else raw_html)
    keep = [ln.strip() for ln in txt.splitlines() if len(ln.strip()) > 50 or re.match(r"^#{0,3}\s*[A-Z].{8,}$", ln.strip())]
    return "\n".join(keep)


async def _read(url: str, chars: int) -> str:
    try:
        r = await asyncio.wait_for(client().get(url), 8)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return ""
        return _main_text(r.text)[:chars]
    except Exception:
        return ""


@tool(risk="low", timeout=60, tags=["search", "google", "web", "internet", "look up", "find online", "news", "latest",
                                    "research", "what is", "who is"],
      examples=["web_search(query='latest nvidia driver')", "web_search(query='rtx 5090 price', read=3)",
                "web_search(query='spacex launch', kind='news')"])
async def web_search(query: str, max_results: int = 8, read: int = 3, kind: str = "web") -> dict:
    """Search the web. Returns titles, links and snippets, and with `read` (default 3) the main text of the top
    pages too - answer from that text. kind: web | news (recent news). Use it for anything current or factual you
    aren't sure of; for one specific page use fetch_page."""
    q = query.strip()
    if not q:
        raise ToolError("Give a `query`.")
    news = kind.lower().startswith("news")
    n = max(1, min(int(max_results), 15))
    results: list[dict] = []
    used = ""
    errors = []
    for name, fn in SOURCES:
        if _blocked.get(name, 0) > time.time():
            continue
        try:
            results = await asyncio.wait_for(fn(q, n, news), 12)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:60] or type(e).__name__}")
            if isinstance(e, httpx.HTTPStatusError) or "403" in str(e) or "429" in str(e) or isinstance(e, asyncio.TimeoutError):
                _blocked[name] = time.time() + 600       # it's refusing us: give it 10 minutes
            results = []
        if results:
            used = name
            break
    if not results:
        raise ToolError("Web search isn't answering right now.",
                        hint="Try again in a minute, or add a Brave Search key in Settings. " + "; ".join(errors)[:200])
    k = max(0, min(int(read), 5))
    if k:
        # some sites refuse non-browsers (shops, MSN...): open a few spares so `read` pages really come back
        texts = await asyncio.gather(*(_read(r["url"], 2500) for r in results[:k + 3]))
        for r, t in zip(results, texts):
            if t and k:
                r["content"] = t
                k -= 1
    return {"query": q, "source": used, "results": results,
            "how": "Answer from the `content` of the pages (and say which site). Snippets can be stale."}

