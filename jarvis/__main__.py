"""Launch Jarvis: local server + frameless desktop window (or your browser with --browser)."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
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


def _replace_running() -> None:
    """Starting Jarvis again (e.g. after an update) replaces the copy that's already running, so the new version
    takes over the same port and the same window spot. Only ever stops a Jarvis that uses this data folder."""
    pidf = config.DATA_DIR / "jarvis.pid"
    try:
        import psutil

        olds: set[int] = set()
        try:
            olds.add(int(pidf.read_text().strip()))
        except (OSError, ValueError):
            pass
        # plus any other copy started from this same install (two quick starts could leave one running that the pid
        # file no longer names - it kept the mic and talked over the new one)
        me = os.getpid()
        family = {me, os.getppid()}
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info["cmdline"] or []).lower()
                if proc.info["pid"] not in family and cmd.rstrip().endswith("-m jarvis")                         and not os.environ.get("JARVIS_DATA_DIR"):
                    olds.add(proc.info["pid"])
            except (psutil.Error, TypeError):
                continue
        for old in olds - family:
            if not psutil.pid_exists(old):
                continue
            proc = psutil.Process(old)
            if "jarvis" in " ".join(proc.cmdline()).lower():
                proc.terminate()
                try:
                    proc.wait(8)
                except psutil.TimeoutExpired:
                    proc.kill()
    except Exception:
        pass
    try:
        pidf.parent.mkdir(parents=True, exist_ok=True)
        pidf.write_text(str(os.getpid()))
    except OSError:
        pass


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

    # Underscore attributes are hidden from pywebview, which otherwise walks the whole
    # native window object when exposing the API to JavaScript.
    def __init__(self) -> None:
        self._window = None

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def close(self) -> None:
        if self._window:
            self._window.destroy()


def _hotkey(window, hotkey: str) -> None:
    """Global hotkey: show the window and start listening."""
    try:
        from pynput import keyboard
    except Exception:  # not installed, or no X display (Wayland / headless Linux)
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
    except Exception:
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

    # Always keep a log file; when started without a console (pythonw / Jarvis.exe) it's the only output.
    log_dir = config.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir / "jarvis.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "uvicorn.access", "openai", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config.load_dotenv()
    if args.no_voice:
        os.environ["JARVIS_NO_VOICE"] = "1"

    from .server.app import app

    _replace_running()
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    if args.headless or args.browser:
        if args.browser:
            threading.Thread(target=lambda: (_wait_up(port), webbrowser.open(url)), daemon=True).start()
        print(f"Jarvis running at {url}")
        server.run()
        return

    srv = threading.Thread(target=server.run, daemon=True)
    srv.start()
    _wait_up(port)
    try:
        import webview

        # keep the page running when the window is covered or minimized: hand control (the air mouse) runs in it
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                              "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion "
                              "--disable-background-timer-throttling --disable-renderer-backgrounding "
                              "--disable-backgrounding-occluded-windows")
        win_api = WindowApi()
        window = webview.create_window(
            "J.A.R.V.I.S.", url, width=1100, height=820, min_size=(520, 600),
            background_color="#050302", frameless=True, easy_drag=False, js_api=win_api,
        )
        win_api._window = window
        s = config.store.load()
        try:
            _tray(window, url)
        except Exception as e:
            logging.getLogger(__name__).warning("tray icon unavailable: %s", e)
        _hotkey(window, s.hotkey)
        webview.start(debug=args.verbose)
    except Exception as e:
        # No desktop window on this system (e.g. Linux without the GTK/Qt bindings pywebview needs):
        # use the browser instead, and keep the server running until Ctrl+C.
        logging.getLogger(__name__).warning("desktop window unavailable (%s); opening Jarvis in your browser", e)
        print(f"Jarvis running at {url}  (Ctrl+C to quit)")
        webbrowser.open(url)
        try:
            while srv.is_alive():
                srv.join(1)
        except KeyboardInterrupt:
            pass
    server.should_exit = True


if __name__ == "__main__":
    main()
