import asyncio

from jarvis.hands import search


def test_falls_through_to_the_next_source_and_reads_pages(monkeypatch):
    async def broken(q, n, news):
        raise RuntimeError("403 blocked")

    async def works(q, n, news):
        return [{"title": f"r{i}", "url": f"https://x/{i}", "snippet": ""} for i in range(5)]

    async def read(url, chars):
        return "" if url.endswith("/0") else "page text " + url

    monkeypatch.setattr(search, "SOURCES", [("a", broken), ("b", works)])
    monkeypatch.setattr(search, "_read", read)
    search._blocked.clear()
    r = asyncio.run(search.web_search("anything", read=2))
    assert r["source"] == "b"
    assert [x.get("content", "")[:4] for x in r["results"][:4]] == ["", "page", "page", ""]
    assert "a" in search._blocked          # the refusing source is skipped for a while


def test_main_text_prefers_the_article():
    html = "<nav>Home About Login</nav><article><p>" + "Real sentence about the topic here. " * 3 + "</p></article>"
    assert "Real sentence" in search._main_text(html) and "Login" not in search._main_text(html)
