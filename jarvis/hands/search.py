"""Web search that keeps working, finds the right thing, and actually reads the results.

Sources, best first (free ones can block automated searches for a while, so there's always another):
  Brave Search API  with a key in Settings (keychain: brave_search_key) - the most reliable, free tier available
  SearXNG           your own instance, `searxng_url` setting
  DuckDuckGo        Lite page (no key) - good at exact names: small businesses, schools, local places
  Bing              RSS feed (no key) - fast, but "corrects" unusual names into unrelated results
  DuckDuckGo        in Jarvis's own hidden browser (if Playwright is installed) - when the plain pages are blocked
Results that don't match the query are not trusted: the next source is tried and the best set is kept.
With read > 0 the most relevant results are opened in parallel and their main text comes back with them, so the model
answers from the pages themselves instead of guessing from two-line snippets. kind="news" searches recent news.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from urllib.parse import quote_plus, unquote

import httpx

from .. import config
from .registry import ToolError, tool
from .web import _ddg_url, client, html_to_text

log = logging.getLogger(__name__)
_blocked: dict[str, float] = {}             # source -> until when to skip it (it refused us recently)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/141.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}
STOP = {"the", "a", "an", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are", "what", "where", "who", "how",
        "near", "me", "best", "latest", "new", "site", "com", "www", "with", "from", "about", "my"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(html.unescape(s or ""))).strip()


def _terms(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 1 and w not in STOP and not w.startswith("site")]


def _score(r: dict, terms: list[str]) -> float:
    """Share of the query's words found in this result (title, link, snippet)."""
    if not terms:
        return 1.0
    hay = " ".join((r.get("title", ""), unquote(r.get("url", "")), r.get("snippet", ""))).lower()
    return sum(1 for t in terms if t in hay) / len(terms)


def relevance(results: list[dict], q: str) -> float:
    """How well a result set matches the query: the average of its best three results."""
    terms = _terms(q)
    top = sorted((_score(r, terms) for r in results), reverse=True)[:3]
    return sum(top) / len(top) if top else 0.0


# ---------------------------------------------------------------------------------------------------- sources
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


def parse_ddg_lite(page: str) -> list[dict]:
    out = []
    # each result: <a ... href="//duckduckgo.com/l/?uddg=..." class='result-link'>Title</a> ... result-snippet ...
    for block in re.split(r"(?=<a[^>]*class=['\"]result-link['\"])|(?=<a[^>]*href=\"[^\"]*\"[^>]*class=['\"]result-link)", page)[1:]:
        a = re.match(r"(?s)<a([^>]*)>(.*?)</a>", block)
        if not a:
            continue
        href = re.search(r"href=\"([^\"]+)\"", a.group(1))
        if not href or "result-link" not in a.group(1):
            continue
        url = _ddg_url(html.unescape(href.group(1)))
        if "duckduckgo.com/y.js" in url or "ad_domain" in url:
            continue                                          # ads
        snip = re.search(r"(?s)class=['\"]result-snippet['\"][^>]*>(.*?)</td>", block)
        out.append({"title": _clean(a.group(2)), "url": url, "snippet": _clean(snip.group(1) if snip else "")})
    return out


async def _ddg_lite(q: str, n: int, news: bool) -> list[dict]:
    if news:
        return []
    r = await client().get("https://lite.duckduckgo.com/lite/", params={"q": q}, headers=HEADERS)
    r.raise_for_status()
    return parse_ddg_lite(r.text)[:n]


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
                link = unquote(m.group(1))
        out.append({"title": _clean(tag("title")), "url": link, "snippet": _clean(tag("description")),
                    "date": tag("pubDate")})
    return out[:n]


_pw: dict = {}


async def _ddg_browser(q: str, n: int, news: bool) -> list[dict]:
    """DuckDuckGo's normal page in a hidden browser - works when the light pages are blocked. Needs Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []
    if "ctx" not in _pw:
        _pw["pw"] = await async_playwright().start()
        try:
            b = await _pw["pw"].chromium.launch(headless=True, channel="chrome",
                                                args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            b = await _pw["pw"].chromium.launch(headless=True)
        _pw["ctx"] = await b.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
    page = await _pw["ctx"].new_page()
    try:
        await page.goto(f"https://duckduckgo.com/?q={quote_plus(q)}&ia={'news' if news else 'web'}",
                        wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("article[data-testid=result]", timeout=8000)
        rows = await page.eval_on_selector_all("article[data-testid=result]", """els => els.map(e => ({
            title: (e.querySelector('[data-testid=result-title-a]') || {}).innerText || '',
            url: (e.querySelector('[data-testid=result-title-a]') || {}).href || '',
            snippet: (e.querySelector('[data-result=snippet]') || {}).innerText || ''}))""")
        return [r for r in rows if r["url"]][:n]
    finally:
        await page.close()


def _tidy(results: list[dict]) -> list[dict]:
    """Drop ads and repeats (same page, or the same title over and over)."""
    out, seen = [], set()
    for r in results:
        u = r.get("url", "")
        if not u.startswith("http") or re.search(r"aclick|/y\.js|ad_domain|ad_provider", u) or r.get("title", "").lower() in ("more info", ""):
            continue
        key = (re.sub(r"^https?://(www\.)?|[/#?].*$", "", u).lower(), r["title"].lower()[:60])
        if key in seen or u.rstrip("/") in seen:
            continue
        seen.update({key, u.rstrip("/")})
        out.append(r)
    return out


SOURCES = [("brave", _brave), ("searxng", _searx), ("duckduckgo", _ddg_lite), ("bing", _bing),
           ("duckduckgo-browser", _ddg_browser)]


# ---------------------------------------------------------------------------------------------------- reading
def _main_text(raw_html: str) -> str:
    """The readable part of a page: <article>/<main> when there is one, without menus, footers and one-word lines."""
    m = re.search(r"(?is)<article[^>]*>(.*)</article>", raw_html) or re.search(r"(?is)<main[^>]*>(.*)</main>", raw_html)
    txt = html_to_text(m.group(1) if m else raw_html)
    keep = [ln.strip() for ln in txt.splitlines()
            if len(ln.strip()) > 50 or re.match(r"^#{0,3}\s*[A-Z].{8,}$", ln.strip())
            or re.search(r"\d{3}[\s.-]\d{3}[\s.-]\d{4}|\b\d{5}\b|@", ln)]     # addresses, phone numbers, emails
    return "\n".join(dict.fromkeys(keep))


async def _read(url: str, chars: int) -> str:
    try:
        r = await asyncio.wait_for(client().get(url, headers=HEADERS), 6)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return ""
        return _main_text(r.text)[:chars]
    except Exception:
        return ""


def _format(q: str, source: str, results: list[dict]) -> str:
    """Plain text for the model (JSON would spend a third of the space on quotes and escapes)."""
    lines = [f"Results for: {q}  (via {source})", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}" + (f"  ({r['date']})" if r.get("date") else ""))
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:300]}")
        if r.get("content"):
            lines.append("   --- page text ---\n   " + r["content"].replace("\n", "\n   "))
        lines.append("")
    lines.append("Answer from these results and page text (name the site). If they don't have it, say so - "
                 "one differently-worded search at most, not a string of them.")
    return "\n".join(lines)


@tool(risk="low", timeout=60, max_chars=12000,
      tags=["search", "google", "web", "internet", "look up", "find online", "news", "latest",
            "research", "what is", "who is", "address", "where is"],
      examples=["web_search(query='latest nvidia driver')", "web_search(query='rtx 5090 price', read=3)",
                "web_search(query='spacex launch', kind='news')"])
async def web_search(query: str, max_results: int = 8, read: int = 3, kind: str = "web") -> str:
    """Search the web. Returns titles, links and snippets, and the main text of the top `read` pages (default 3) -
    answer from that text. kind: web | news (recent news). Use it for anything current or factual you aren't sure
    of; for one specific page use fetch_page."""
    q = query.strip()
    if not q:
        raise ToolError("Give a `query`.")
    news = kind.lower().startswith("news")
    n = max(1, min(int(max_results), 15))
    best: tuple[float, str, list[dict]] = (-1.0, "", [])
    errors = []
    for name, fn in SOURCES:
        if _blocked.get(name, 0) > time.time():
            continue
        try:
            results = await asyncio.wait_for(fn(q, n, news), 15)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:60] or type(e).__name__}")
            if isinstance(e, (httpx.HTTPStatusError, httpx.TransportError, asyncio.TimeoutError)) \
                    or "403" in str(e) or "429" in str(e):
                _blocked[name] = time.time() + 600       # it's refusing us: give it 10 minutes
            continue
        results = _tidy(results)
        if not results:
            continue
        rel = relevance(results, q)
        if rel > best[0]:
            best = (rel, name, results)
        if rel >= 0.6 or news:                           # good enough - otherwise see if another source does better
            break
    rel, used, results = best
    if not results:
        raise ToolError("Web search isn't answering right now.",
                        hint="Try again in a minute, or add a Brave Search key in Settings. " + "; ".join(errors)[:200])
    terms = _terms(q)
    k = max(0, min(int(read), 5))
    if k:
        # read the most relevant pages first; some sites refuse non-browsers (shops, MSN...), so open a few spares
        order = sorted(range(len(results)), key=lambda i: (-_score(results[i], terms), i))[:k + 3]
        texts = await asyncio.gather(*(_read(results[i]["url"], 2000) for i in order))
        for i, t in zip(order, texts):
            if t and k:
                results[i]["content"] = t
                k -= 1
    out = _format(q, used, results)
    if rel < 0.4:
        out += ("\n(These results only loosely match the query - the exact name may be spelled differently. Try the "
                "spelling from any result that looks right, or tell the user what you found.)")
    return out
