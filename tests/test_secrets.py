import os
import sys

import pytest

from jarvis import config


@pytest.fixture
def no_keychain(tmp_path, monkeypatch):
    import keyring

    def boom(*a, **k):
        raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr(keyring, "set_password", boom)
    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(keyring, "delete_password", boom)
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.json")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    return tmp_path / "secrets.json"


def test_api_key_falls_back_to_private_file(no_keychain):
    config.set_api_key("  nvapi-test  ")
    assert config.get_api_key() == "nvapi-test"
    assert no_keychain.exists()
    if sys.platform != "win32":
        assert (os.stat(no_keychain).st_mode & 0o777) == 0o600
    config.set_secret("camera:x", "rtsp://u:p@h/1")
    assert config.get_secret("camera:x") == "rtsp://u:p@h/1"
    config.set_api_key("")
    assert config.get_api_key() == ""
    assert config.get_secret("camera:x") == "rtsp://u:p@h/1"
