import asyncio

import numpy as np
import pytest

from jarvis import config
from jarvis.memory import contentindex as ci


@pytest.fixture
def docs(tmp_path, monkeypatch):
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "lab.md").write_text("Photosynthesis lab: elodea released oxygen bubbles under the lamp.")
    (d / "budget.csv").write_text("item,cost\nGPU,4500")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "skip.md").write_text("photosynthesis should not be indexed here")
    monkeypatch.setattr(ci, "DB", tmp_path / "idx.db")
    config.store.update(index_folders=[str(d)])
    ci._matrix.clear()
    yield d
    config.store.update(index_folders=[])


def test_keyword_search_and_incremental(docs, monkeypatch):
    async def no_embed():
        return ""

    monkeypatch.setattr(ci, "_embed_model", no_embed)
    assert ci.build()["indexed"] == 2                      # node_modules skipped
    assert ci.build()["indexed"] == 0                      # nothing changed
    hits = asyncio.run(ci.search("oxygen experiment with plants"))
    assert hits[0]["name"] == "lab" and "[oxygen]" in hits[0]["snippet"].lower()
    (docs / "budget.csv").unlink()
    assert ci.build()["removed"] == 1


def test_meaning_hits_without_shared_words(docs, monkeypatch):
    vecs = {"lab": [1, 0, 0], "budget": [0, 1, 0]}

    def fake_embed(texts, model):
        out = []
        for t in texts:
            t = t.lower()
            v = vecs["budget"] if ("budget" in t or "graphics" in t) else vecs["lab"] if ("lab" in t or "plants" in t) else [0, 0, 1]
            out.append(v)
        return np.array(out, dtype=np.float32)

    async def model():
        return "fake-embed"

    monkeypatch.setattr(ci, "_embed", fake_embed)
    monkeypatch.setattr(ci, "_embed_model", model)
    ci.build()
    assert ci.embed_pending("fake-embed") == 2
    hits = asyncio.run(ci.search("graphics card"))         # no keyword in common with the CSV
    assert [h["name"] for h in hits] == ["budget"]
