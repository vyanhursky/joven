# PyInstaller spec for the downloadable app.  Build with:
#
#     python packaging/fetch_vendor.py
#     pyinstaller packaging/joven.spec --noconfirm
#
# `joven/ui/page.html` is read through `importlib.resources`, which is invisible
# to the import graph. Without declaring it the server answers every request with
# a traceback.
#
# `lingua` needs no help: its language models are compiled into the extension
# module itself (a single 291 MB .pyd/.so), not shipped as package data, so
# PyInstaller's normal binary collection catches them. The collect_* calls below
# are belt and braces and cost nothing. That 291 MB is also why the bundle is the
# size it is, and why it cannot be trimmed by selecting languages — measured on
# Windows 2026-09-17: 357 MB on disk, 220 MB zipped.
#
# The build is onedir, not onefile. onefile unpacks the whole bundle to a temp
# directory on *every* launch, which at this size is a wait a reader reads as a
# hang; onedir starts in ~260 ms. See docs/releasing.md.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
VENDOR = SPEC_DIR / "vendor"

if not VENDOR.is_dir():
    raise SystemExit(
        f"{VENDOR} is missing — run: python packaging/fetch_vendor.py"
    )

datas = [
    # (source, destination-inside-the-bundle)
    (str(SPEC_DIR.parent / "src" / "joven" / "ui" / "page.html"), "joven/ui"),
    (str(SPEC_DIR / "NOTICE"), "."),
]
# Everything vendor/ holds, at the path joven.external expects to find it.
for path in VENDOR.rglob("*"):
    if path.is_file():
        datas.append((str(path), str(Path("vendor") / path.parent.relative_to(VENDOR))))

datas += collect_data_files("lingua")

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(SPEC_DIR.parent / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("lingua"),
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a window; excluding the GUI toolkits keeps tkinter and
    # its DLLs out of a bundle that is already large.
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Joven",
    debug=False,
    strip=False,
    upx=False,
    # A console is the right default even though most readers never look at it:
    # it is where a crash before the browser opens becomes visible, and `Joven.exe
    # detect book.epub` still has to work for everyone else.
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Joven",
)

# macOS wants something to double-click, and a bare Unix executable is not it.
import sys

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Joven.app",
        icon=None,
        bundle_identifier="com.vyanhursky.joven",
        info_plist={
            "CFBundleName": "Joven",
            "CFBundleDisplayName": "Joven",
            "NSHighResolutionCapable": True,
            # Nothing here has a Cocoa UI; without this the Dock shows a window
            # that never arrives while the real interface is in the browser.
            "LSBackgroundOnly": False,
        },
    )
