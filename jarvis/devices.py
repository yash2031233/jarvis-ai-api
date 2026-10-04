"""Find the devices Jarvis can use - and make sure each one really is what it looks like.

One quick sweep of the local network (a few seconds), then every candidate is asked to identify itself before it's
trusted - an open port alone is never enough (routers, TVs and NAS boxes have web servers too):
  3D printers   Flashforge (answers ~M115 with its machine type), Klipper/Moonraker (/server/info), OctoPrint
                (its own web page / API)
  robot car     the jarvis-car firmware (its "here I am" broadcast, then /ping must say jarvis-car)
  cameras       webcams that actually deliver a picture; IP cameras that answer ONVIF discovery
  audio         microphones and speakers
Found devices are set up by themselves when there's nothing configured yet (and re-found when their address
changes), so nobody has to type an IP address.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

PORTS = (8899, 7125, 80, 5000)
CAR_BEACON = 47800


def local_subnets() -> list[str]:
    """The /24 networks this computer is on (the main one first; Tailscale's 100.x skipped)."""
    nets: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        nets.append(".".join(s.getsockname()[0].split(".")[:3]))
        s.close()
    except OSError:
        pass
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and re.match(r"^(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.", a.address):
                    n = ".".join(a.address.split(".")[:3])
                    if n not in nets:
                        nets.append(n)
    except Exception:
        pass
    return nets[:3]


def _open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ identify
def flashforge_info(host: str) -> dict | None:
    """Ask a Flashforge on its LAN text port who it is: 'Machine Type: Flashforge AD5X' etc."""
    try:
        with socket.create_connection((host, 8899), timeout=2) as s:
            s.settimeout(2)

            def cmd(c: str) -> str:
                s.sendall((c + "\r\n").encode())
                time.sleep(0.2)
                buf = b""
                try:
                    while b"ok" not in buf[-16:]:
                        b = s.recv(4096)
                        if not b:
                            break
                        buf += b
                except socket.timeout:
                    pass
                return buf.decode(errors="replace")
            cmd("~M601 S1")
            info = cmd("~M115")
            s.sendall(b"~M602\r\n")
    except OSError:
        return None

    def g(pat: str):
        m = re.search(pat, info)
        return m.group(1).strip() if m else None
    mtype = g(r"Machine Type\s*:\s*(.+)")
    if not mtype:
        return None
    model = mtype if mtype.lower().startswith("flashforge") else f"Flashforge {mtype}"
    return {"kind": "flashforge", "host": host, "model": model, "name": g(r"Machine Name\s*:\s*(.+)"),
            "firmware": g(r"Firmware\s*:\s*(.+)"), "serial": g(r"SN\s*:\s*(\S+)")}


def moonraker_info(host: str) -> dict | None:
    try:
        r = httpx.get(f"http://{host}:7125/server/info", timeout=2).json().get("result", {})
        if "klippy_state" not in r:
            return None
        name = httpx.get(f"http://{host}:7125/printer/info", timeout=2).json().get("result", {}).get("hostname")
        return {"kind": "moonraker", "host": host, "model": name or "Klipper printer"}
    except Exception:
        return None


def octoprint_info(host: str, port: int) -> dict | None:
    try:
        r = httpx.get(f"http://{host}:{port}/", timeout=2, follow_redirects=True)
        if "octoprint" not in r.text[:20000].lower():
            return None
        return {"kind": "octoprint", "host": host if port == 80 else f"{host}:{port}", "model": "OctoPrint"}
    except Exception:
        return None


def car_info(host: str) -> dict | None:
    try:
        d = httpx.get(f"http://{host}/ping", timeout=1.2).json()
        return {"kind": "car", "host": host, "model": d.get("name"), "firmware": d.get("fw")} \
            if d.get("name") == "jarvis-car" else None
    except Exception:
        return None


def hear_car(seconds: float = 3.0) -> str | None:
    """The jarvis-car broadcasts {"name": "jarvis-car"} every 2 s - works whatever address the router gave it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", CAR_BEACON))
        s.settimeout(0.5)
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, (sender, _) = s.recvfrom(512)
            except socket.timeout:
                continue
            try:
                if json.loads(data).get("name") == "jarvis-car":
                    return sender
            except ValueError:
                continue
    except OSError:
        return None
    finally:
        s.close()
    return None


def onvif_cameras(seconds: float = 2.5) -> list[dict]:
    """IP cameras answering ONVIF (WS-Discovery) - most network cameras do."""
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
    out: dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.4)
        s.sendto(probe.encode(), ("239.255.255.250", 3702))
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, (ip, _) = s.recvfrom(65535)
            except socket.timeout:
                continue
            txt = data.decode(errors="replace")
            types = " ".join(re.findall(r"<[^>]*Types>([^<]+)<", txt))
            addrs = re.findall(r"<[^>]*XAddrs>([^<]+)<", txt)
            # other things answer WS-Discovery too (Windows PCs, NAS boxes, printers): a camera says it's a
            # NetworkVideoTransmitter and serves the ONVIF device service
            if "NetworkVideoTransmitter" not in types or not any("onvif" in a.lower() for a in addrs):
                continue
            scopes = " ".join(re.findall(r"<[^>]*Scopes>([^<]+)<", txt))
            name = re.search(r"onvif://www\.onvif\.org/name/([^\s]+)", scopes)
            hw = re.search(r"onvif://www\.onvif\.org/hardware/([^\s]+)", scopes)
            out[ip] = {"kind": "ip_camera", "host": ip, "onvif": addrs[0].split()[0] if addrs else None,
                       "model": " ".join(x.group(1).replace("%20", " ") for x in (name, hw) if x) or "ONVIF camera",
                       "rtsp_guess": f"rtsp://{ip}:554/"}
    except OSError:
        pass
    finally:
        s.close()
    return list(out.values())


# ------------------------------------------------------------------ the sweep
def scan(include_cameras: bool = True) -> dict[str, Any]:
    t0 = time.time()
    hosts = [f"{n}.{i}" for n in local_subnets() for i in range(1, 255)]
    pairs = [(h, p) for h in hosts for p in PORTS]
    with ThreadPoolExecutor(max_workers=256) as ex:
        opened = [hp for hp, ok in zip(pairs, ex.map(lambda hp: _open(*hp), pairs)) if ok]
        car_ip = ex.submit(hear_car, 2.5)
        cams_ip = ex.submit(onvif_cameras) if include_cameras else None
        checks = []
        for h, p in opened:
            if p == 8899:
                checks.append(ex.submit(flashforge_info, h))
            elif p == 7125:
                checks.append(ex.submit(moonraker_info, h))
            elif p in (80, 5000):
                checks.append(ex.submit(octoprint_info, h, p))
                if p == 80:
                    checks.append(ex.submit(car_info, h))
        found = [c.result() for c in checks]
        car_beacon = car_ip.result()
        ip_cams = cams_ip.result() if cams_ip else []
    printers = [d for d in found if d and d["kind"] in ("flashforge", "moonraker", "octoprint")]
    cars = [d for d in found if d and d["kind"] == "car"]
    if car_beacon and not any(c["host"] == car_beacon for c in cars):
        c = car_info(car_beacon)
        if c:
            cars.append(c)
    out: dict[str, Any] = {"printers": printers, "cars": cars, "ip_cameras": ip_cams,
                           "networks": [f"{n}.0/24" for n in local_subnets()], "seconds": round(time.time() - t0, 1)}
    if include_cameras:
        try:
            from .vision import cameras

            out["webcams"] = cameras.detect_devices()
        except Exception:
            out["webcams"] = []
    try:
        from .voice.pipeline import list_input_devices, list_output_devices

        out["microphones"] = list(dict.fromkeys(d["name"] for d in list_input_devices()))
        out["speakers"] = list_output_devices()
    except Exception:
        pass
    return out


def _slicer_model(model: str) -> str:
    """'Flashforge AD5X' -> the slicer's profile for it ('Flashforge AD5X 0.4 nozzle') when there is one."""
    try:
        from .cad.printer import _profiles_root, slicer_path

        root = _profiles_root(slicer_path())
        names = [p.stem for p in root.glob("*/machine/*.json")]
        for cand in (f"{model} 0.4 nozzle", model):
            hit = next((n for n in names if n.lower() == cand.lower()), None)
            if hit:
                return hit
    except Exception:
        pass
    return model


def apply(found: dict[str, Any], force: bool = False) -> list[str]:
    """Set up what was found when nothing is set up yet (or the saved address moved). Returns what changed."""
    s = config.store.load()
    changes: dict[str, Any] = {}
    said: list[str] = []
    printers = found.get("printers") or []
    same = [p for p in printers if p["kind"] == s.printer_kind] if s.printer_kind else printers
    if same and (force or not s.printer_host or s.printer_host not in [p["host"] for p in printers]):
        p = same[0]
        if len(same) == 1 or force:
            changes.update(printer_kind=p["kind"], printer_host=p["host"])
            if not s.printer_model or force:
                changes["printer_model"] = _slicer_model(p["model"]) if p["kind"] == "flashforge" else s.printer_model
            said.append(f"3D printer: {p['model']} at {p['host']}")
    cars = found.get("cars") or []
    if cars and (force or s.robot_host not in [c["host"] for c in cars]):
        changes["robot_host"] = cars[0]["host"]
        said.append(f"robot car at {cars[0]['host']}")
    if changes:
        config.store.update(**{k: v for k, v in changes.items() if v is not None})
    return said


def auto_setup() -> None:
    """At start: find devices in the background and set up whatever isn't set up yet."""
    try:
        said = apply(scan(include_cameras=False))
        if said:
            log.info("devices found: %s", "; ".join(said))
            from .events import bus

            bus.emit("notice", level="info", text="Found " + "; ".join(said))
    except Exception as e:
        log.info("device scan failed: %s", e)
