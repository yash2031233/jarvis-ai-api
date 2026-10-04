# PyInstaller spec — builds a standalone Jarvis app (one folder; .app bundle on macOS).
# Run from the repo root:  pyinstaller packaging/jarvis.spec --noconfirm
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

hidden = (
    collect_submodules("jarvis")
    + collect_submodules("uvicorn")
    + ["keyring.backends", "pyperclip", "pystray", "pynput"]
)
datas = [
    (str(ROOT / "jarvis" / "ui"), "jarvis/ui"),
    (str(ROOT / "jarvis" / "cad" / "lib"), "jarvis/cad/lib"),
    (str(ROOT / "jarvis" / "vision" / "winocr.ps1"), "jarvis/vision"),
    (str(ROOT / "plugins"), "plugins"),
]
for pkg in ("faster_whisper", "kokoro_onnx", "openwakeword", "language_tags", "espeakng_loader", "phonemizer"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "torch"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Jarvis",
    console=False,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Jarvis")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Jarvis.app",
        bundle_identifier="io.github.jarvis-ai-api",
        info_plist={"NSMicrophoneUsageDescription": "Jarvis listens for your voice commands.",
                    "NSAppleEventsUsageDescription": "Jarvis controls apps you ask it to."},
    )
