import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest


def test_robot_car_shows_up_as_a_camera(monkeypatch):
    cv2 = pytest.importorskip("cv2")
    from jarvis import config
    from jarvis.vision import cameras

    jpg = cv2.imencode(".jpg", np.full((48, 64, 3), 200, np.uint8))[1].tobytes()
    seen = []

    class Car(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("X-Token"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(jpg + bytes(8))      # the car pads its frames with zeros

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Car)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "get_secret", lambda n: "tok" if n == "robot_token" else "")
    config.store.update(robot_host=f"127.0.0.1:{srv.server_port}")
    try:
        cams = {c.id: c for c in cameras.hub.all()}
        assert cams["car"].name == "Robot car" and cams["car"].kind == "car"
        img = cameras.hub.get("robot car").frame()
        assert img.shape == (48, 64, 3) and seen[-1] == "tok"
    finally:
        config.store.update(robot_host="")
        srv.shutdown()
    assert "car" not in {c.id for c in cameras.hub.all()}
