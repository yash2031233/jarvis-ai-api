"""Launch Jarvis: local server + frameless desktop window (or your browser with --browser)."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from . import __version__, config


def _port_in_use(port: int) -> bool:
    # On Windows, binding 127.0.0.1 can succeed even when another app listens on 0.0.0.0,
    # so also check whether something already answers on the port.
    with socket.socket() as s:
        s.settimeout(0.2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        if not _port_in_use(port):
            return port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(port: int, timeout: float = 20) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)


class WindowApi:
    """Exposed to the UI as window.pywebview.api (frameless window controls)."""

    def __init__(self) -> None:
        self.window = None

    def minimize(self) -> None:
        if self.window:
            self.window.minimize()

    def close(self) -> None:
        if self.window:
            self.window.destroy()


def _hotkey(window, hotkey: str) -> None:
    """Global hotkey: show the window and start listening."""
    try:
        from pynput import keyboard
    except ImportError:
        return
    combo = "+".join(f"<{p}>" if len(p) > 1 else p for p in hotkey.lower().split("+"))

    def fire():
        try:
            window.show()
            window.restore()
            window.evaluate_js("window.jarvis && window.jarvis.pushToTalk()")
        except Exception:
            pass

    try:
        keyboard.GlobalHotKeys({combo: fire}).start()
    except Exception as e:
        logging.getLogger(__name__).warning("hotkey %s unavailable: %s", hotkey, e)


def _tray(window, url: str) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), outline=(240, 170, 90, 255), width=4)
    d.ellipse((22, 22, 42, 42), fill=(255, 200, 140, 255))

    def show(icon, item):
        window.show()
        window.restore()

    def hide(icon, item):
        window.hide()

    def quit_(icon, item):
        icon.stop()
        window.destroy()

    icon = pystray.Icon("jarvis", img, "Jarvis", menu=pystray.Menu(
        pystray.MenuItem("Show", show, default=True), pystray.MenuItem("Hide", hide),
        pystray.MenuItem("Open in browser", lambda i, it: webbrowser.open(url)),
        pystray.MenuItem("Quit", quit_)))
    threading.Thread(target=icon.run, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser(prog="jarvis", description="J.A.R.V.I.S. desktop assistant")
    ap.add_argument("--browser", action="store_true", help="open the UI in your web browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="server only (no window)")
    ap.add_argument("--port", type=int, default=47821)
    ap.add_argument("--no-voice", action="store_true", help="disable voice for this run")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"jarvis {__version__}")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "uvicorn.access", "openai", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config.load_dotenv()
    if args.no_voice:
        os.environ["JARVIS_NO_VOICE"] = "1"

    from .server.app import app

    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    if args.headless or args.browser:
        if args.browser:
            threading.Thread(target=lambda: (_wait_up(port), webbrowser.open(url)), daemon=True).start()
        print(f"Jarvis running at {url}")
        server.run()
        return

    threading.Thread(target=server.run, daemon=True).start()
    _wait_up(port)
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        server.should_exit = True
        return

    win_api = WindowApi()
    window = webview.create_window(
        "J.A.R.V.I.S.", url, width=1100, height=820, min_size=(520, 600),
        background_color="#050302", frameless=True, easy_drag=False, js_api=win_api,
    )
    win_api.window = window
    s = config.store.load()
    _tray(window, url)
    _hotkey(window, s.hotkey)
    webview.start(debug=args.verbose)
    server.should_exit = True


if __name__ == "__main__":
    main()
