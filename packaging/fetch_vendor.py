"""Download the binaries the packaged app ships with.

Joven shells out to two tools that Python should not reimplement, and the whole
point of the downloadable app is that a reader installs nothing by hand. So the
build fetches them and :mod:`joven.external` prefers the bundled copy.

    python packaging/fetch_vendor.py                 # kepubify and epubcheck
    python packaging/fetch_vendor.py --no-epubcheck  # kepubify only

**kepubify** is a single static Go binary with no runtime, so it always ships:
without it there is no KEPUB, and the KEPUB is what the Kobo wants.

**epubcheck** is a JAR, and a JAR needs a JVM that the app cannot bundle. It
ships anyway, at the cost of about 32 MB, because a reader who *does* have Java
then gets the real validation instead of ``SKIPPED`` without setting anything —
and ``--no-epubcheck`` exists for when that trade stops being worth it. The whole
distribution is unpacked, not just the jar: the jar's manifest ``Class-Path``
points at ``lib/``, and without those 37 jars epubcheck does not start. The
distribution also carries its own ``LICENSE.txt``, ``THIRD-PARTY.txt`` and
``licenses/``, which is what we are obliged to redistribute alongside it.

Both licences permit redistribution with attribution: kepubify is MIT, epubcheck
is BSD-3-Clause. See ``packaging/NOTICE``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

KEPUBIFY_VERSION = "4.0.4"
EPUBCHECK_VERSION = "5.1.0"

KEPUBIFY_URL = "https://github.com/pgaskin/kepubify/releases/download/v{v}/{asset}"
EPUBCHECK_URL = (
    "https://github.com/w3c/epubcheck/releases/download/v{v}/epubcheck-{v}.zip"
)

# kepubify publishes one bare binary per platform, named for the target.
KEPUBIFY_ASSETS = {
    ("Windows", "AMD64"): "kepubify-windows-64bit.exe",
    ("Windows", "ARM64"): "kepubify-windows-arm64.exe",
    ("Linux", "x86_64"): "kepubify-linux-64bit",
    ("Linux", "aarch64"): "kepubify-linux-arm64",
    ("Darwin", "x86_64"): "kepubify-darwin-64bit",
    ("Darwin", "arm64"): "kepubify-darwin-arm64",
}


def _asset_for_host() -> str:
    key = (platform.system(), platform.machine())
    if key not in KEPUBIFY_ASSETS:
        raise SystemExit(
            f"no kepubify build for {key[0]}/{key[1]} — "
            f"known: {', '.join(f'{s}/{m}' for s, m in KEPUBIFY_ASSETS)}"
        )
    return KEPUBIFY_ASSETS[key]


def _download(url: str) -> bytes:
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https
        data: bytes = response.read()
    print(f"  {len(data):,} bytes  sha256 {hashlib.sha256(data).hexdigest()[:16]}…")
    return data


def fetch_kepubify(vendor: Path) -> Path:
    asset = _asset_for_host()
    data = _download(KEPUBIFY_URL.format(v=KEPUBIFY_VERSION, asset=asset))
    # The name matters: joven.external looks up "kepubify" (and "kepubify.exe"),
    # not whatever the release asset happened to be called.
    target = vendor / ("kepubify.exe" if asset.endswith(".exe") else "kepubify")
    target.write_bytes(data)
    if not asset.endswith(".exe"):
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  -> {target}")
    return target


def fetch_epubcheck(vendor: Path) -> Path:
    data = _download(EPUBCHECK_URL.format(v=EPUBCHECK_VERSION))
    target = vendor / "epubcheck"
    if target.exists():
        shutil.rmtree(target)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # The zip has a single epubcheck-<version>/ root; flatten it so the path
        # joven.verify looks for does not carry a version number.
        root = f"epubcheck-{EPUBCHECK_VERSION}/"
        members = [n for n in zf.namelist() if n.startswith(root) and not n.endswith("/")]
        if not members:
            raise SystemExit(f"{root} not found in the epubcheck zip")
        for name in members:
            destination = target / name[len(root) :]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(zf.read(name))
    jar = target / "epubcheck.jar"
    if not jar.is_file():
        raise SystemExit(f"epubcheck.jar missing from {target}")
    unpacked = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"  -> {target} ({unpacked:,} bytes)")
    return jar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendor",
        type=Path,
        default=Path(__file__).resolve().parent / "vendor",
        help="where to put them (default: packaging/vendor)",
    )
    parser.add_argument(
        "--no-epubcheck",
        action="store_true",
        help="skip epubcheck, saving about 32 MB; verify then reports it SKIPPED",
    )
    args = parser.parse_args(argv)

    vendor: Path = args.vendor
    vendor.mkdir(parents=True, exist_ok=True)
    print(f"vendoring into {vendor}")

    print("kepubify:")
    fetch_kepubify(vendor)
    if args.no_epubcheck:
        print("epubcheck: skipped")
    else:
        print("epubcheck:")
        fetch_epubcheck(vendor)

    total = sum(f.stat().st_size for f in vendor.rglob("*") if f.is_file())
    print(f"\nvendor total: {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
