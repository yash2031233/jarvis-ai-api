from jarvis import config, media


def test_media_tags_are_split_from_the_text():
    clean, refs = media.split("Here's the door.\nMEDIA:/api/camera/snap/door_1.jpg\nAnd the part: MEDIA:C:\\parts\\x.png.")
    assert refs == ["/api/camera/snap/door_1.jpg", "C:\\parts\\x.png"]
    assert "MEDIA" not in clean and clean.startswith("Here's the door.")


def test_media_refs_resolve_to_real_files_only(tmp_path):
    snap = config.DATA_DIR / "camera" / "t_1.jpg"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"\xff\xd8x\xff\xd9")
    other = tmp_path / "plot.png"
    other.write_bytes(b"png")
    assert media.resolve("/api/camera/snap/t_1.jpg?token=x") == snap.resolve()
    assert media.resolve(str(other)) == other.resolve()
    assert media.resolve("/api/camera/snap/missing.jpg") is None
    assert media.resolve("/api/camera/snap/../../secret.txt") is None
    assert media.kind(other) == "image"


def test_telegram_attaches_media(tmp_path, monkeypatch):
    from jarvis import telegram

    pic = tmp_path / "car.jpg"
    pic.write_bytes(b"\xff\xd8x\xff\xd9")
    calls = []

    class R:
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(telegram, "chat_id", lambda: 42)
    monkeypatch.setattr(telegram, "token", lambda: "t")
    monkeypatch.setattr(telegram.httpx, "post", lambda url, **kw: calls.append((url.rsplit("/", 1)[1], kw)) or R())
    monkeypatch.setattr(telegram, "_call", lambda method, **kw: calls.append((method, kw)))
    assert telegram.send(f"The car sees this.\nMEDIA:{pic}")
    method, kw = calls[0]
    assert method == "sendPhoto" and kw["data"]["caption"] == "The car sees this." and "photo" in kw["files"]
