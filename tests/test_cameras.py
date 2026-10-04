"""Camera setup logic (no camera hardware needed)."""

import numpy as np
import pytest

from jarvis import config
from jarvis.vision import cameras


@pytest.fixture
def fake_keychain(monkeypatch):
    store = {}
    monkeypatch.setattr(config, "get_secret", lambda n: store.get(n, ""))
    monkeypatch.setattr(config, "set_secret", lambda n, v: store.__setitem__(n, v) if v else store.pop(n, None))
    config.store.update(cameras=[], default_camera="", auto_webcam=True)
    cameras.hub.cams.clear()
    yield store
    config.store.update(cameras=[], default_camera="", auto_webcam=True)


def test_mask_url():
    assert config.mask_url("rtsp://admin:hunter2@192.168.1.9:554/s1") == "rtsp://admin:••••@192.168.1.9:554/s1"
    assert config.mask_url("http://cam.local/snap.jpg") == "http://cam.local/snap.jpg"


def test_auto_webcam_stays_when_ip_camera_added(fake_keychain):
    assert [c.id for c in cameras.hub.all()] == ["webcam"]
    e = cameras.add_camera("Front door", "ip", url="rtsp://admin:pw@10.0.0.5/stream")
    ids = [c.id for c in cameras.hub.all()]
    assert ids == ["webcam", "front-door"]
    assert config.store.load().default_camera == ""          # the webcam is still the default
    assert cameras.hub.get("").id == "webcam"
    assert fake_keychain["camera:front-door"] == "rtsp://admin:pw@10.0.0.5/stream"   # secret not in settings
    assert "pw" not in str(config.store.load().cameras) and "••••" in e["url_display"]
    assert cameras.hub.get("front").id == "front-door"        # fuzzy by name


def test_direct_camera_replaces_auto_and_remove(fake_keychain):
    cameras.add_camera("Desk", "device", device=1)
    assert [c.id for c in cameras.hub.all()] == ["desk"]
    cameras.remove_camera("desk")
    assert [c.id for c in cameras.hub.all()] == ["webcam"]
    cameras.remove_camera("webcam")                           # hide the auto webcam
    assert cameras.hub.all() == []
    with pytest.raises(cameras.CameraError):
        cameras.hub.get("")


def test_rejects_bad_ip_address(fake_keychain):
    with pytest.raises(cameras.CameraError):
        cameras.add_camera("x", "ip", url="192.168.1.5")


def test_snapshot_url_detection():
    assert cameras.Camera._is_snapshot("http://cam/snapshot.jpg")
    assert cameras.Camera._is_snapshot("http://cam/cgi-bin/snapshot.cgi")


def test_change_detection_box():
    from jarvis.hands.camera import _changed_box

    a = np.zeros((54, 96), np.int16)
    b = a.copy()
    b[10:20, 40:60] = 200
    share, (x0, y0, x1, y1) = _changed_box(b, a, (720, 1280))
    assert 0.03 < share < 0.06
    assert x0 < 40 * 1280 / 96 < x1 and y0 < 10 * 720 / 54 < y1


def test_ocr_find_prefers_exact_labels():
    from jarvis.vision import ocr

    def ln(text, x, y):
        words, cx = [], x
        for w in text.split():
            words.append({"t": w, "x": cx, "y": y, "w": len(w) * 8, "h": 14})
            cx += len(w) * 8 + 6
        return {"text": text, "x": x, "y": y, "w": cx - x, "h": 14, "words": words}

    lines = [ln("https://example.com/settings/integrations", 0, 0), ln("Integrations", 500, 200), ln("Save", 40, 300)]
    hits = ocr.find(lines, "integrations")
    assert hits[0]["text"] == "Integrations" and hits[0]["x"] > 500
    assert ocr.find(lines, "Save")[0]["y"] == 307
    assert ocr.text_of(lines).splitlines()[1] == "Integrations"
