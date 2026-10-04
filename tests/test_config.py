from jarvis import config


def test_mask_key():
    assert config.mask_key("") == ""
    assert config.mask_key("nvapi-abcdefghijklmnop1234") == "nvapi-••••1234"
    assert "abcdefgh" not in config.mask_key("sk-abcdefghijklmnop")


def test_settings_roundtrip():
    s = config.store.update(model="meta/llama-3.3-70b-instruct", tts_speed=1.1)
    config.store._settings = None  # force re-read from disk
    s2 = config.store.load()
    assert s2.model == s.model and s2.tts_speed == 1.1


def test_settings_file_has_no_key():
    config.store.update(model="x")
    assert "nvapi" not in config.SETTINGS_FILE.read_text()
