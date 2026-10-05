import asyncio

from jarvis.hands import search


def _results(titles):
    return [{"title": t, "url": f"https://site{i}.example/{i}", "snippet": ""} for i, t in enumerate(titles)]


def test_falls_through_to_the_next_source_and_reads_pages(monkeypatch):
    async def broken(q, n, news):
        raise RuntimeError("403 blocked")

    async def works(q, n, news):
        return _results([f"blue widget review {i}" for i in range(5)])

    async def read(url, chars):
        return "" if url.endswith("/0") else "page text " + url

    monkeypatch.setattr(search, "SOURCES", [("a", broken), ("b", works)])
    monkeypatch.setattr(search, "_read", read)
    search._blocked.clear()
    out = asyncio.run(search.web_search("blue widget review", read=2))
    assert "(via b)" in out
    assert out.count("--- page text ---") == 2          # the unreadable page was skipped for a spare
    assert "a" in search._blocked                        # the refusing source is skipped for a while


def test_results_that_ignore_the_query_dont_win(monkeypatch):
    """Bing "corrects" an unusual name (Zorbo Dance -> Zorba) into unrelated pages - the next source has the real thing."""
    async def wrong(q, n, news):
        return _results(["Zorba - jewelry", "Zorba the Greek - Wikipedia", "Zorba recipes"])

    async def right(q, n, news):
        return _results(["Zorbo Dance Studio - dance school, Springfield", "Zorbo Dance Studio classes"])

    async def read(url, chars):
        return ""

    monkeypatch.setattr(search, "SOURCES", [("bing", wrong), ("ddg", right)])
    monkeypatch.setattr(search, "_read", read)
    search._blocked.clear()
    out = asyncio.run(search.web_search("zorbo dance studio springfield"))
    assert "(via ddg)" in out and "jewelry" not in out


def test_parses_duckduckgo_lite_and_drops_ads():
    page = """
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fschool&amp;rut=1" class='result-link'>The School</a>
      <td class='result-snippet'>Founded in <b>2009</b>.</td>
      <a rel="nofollow" href="https://duckduckgo.com/y.js?ad_domain=x" class='result-link'>more info</a>
      <a class='result-link' href="https://example.com/b">Second</a>
    """
    rows = search._tidy(search.parse_ddg_lite(page))
    assert [r["title"] for r in rows] == ["The School", "Second"]
    assert rows[0]["url"] == "https://example.org/school" and rows[0]["snippet"] == "Founded in 2009 ."


def test_main_text_prefers_the_article_and_keeps_addresses():
    page = ("<nav>Home About Login</nav><article><p>" + "Real sentence about the topic here. " * 3 +
            "</p><p>12 Main St, Springfield 01101</p></article>")
    txt = search._main_text(page)
    assert "Real sentence" in txt and "Login" not in txt and "01101" in txt
