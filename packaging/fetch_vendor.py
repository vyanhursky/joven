"""Download the binary the packaged app ships with.

    python packaging/fetch_vendor.py

**kepubify** is a single static Go binary with no runtime, so it ships: without
it there is no KEPUB, and the KEPUB is what the Kobo wants. Its licence permits
redistribution with attribution; see ``packaging/NOTICE``.

**epubcheck** used to ship here too, and no longer does. It is a JAR, and a JAR
needs a JVM the app cannot bundle — so those 32 MB, a seventh of the download,
did nothing for the majority of readers who have no Java, while the two Setup
rows explaining its absence were the most confusing thing on the page. It stays a
development and CI dependency, where it has earned its place: it is the only gate
that validates the output as an EPUB rather than as a diff of the input, and it
caught twenty real conformance errors in the EPUB 3 upgrade. A reader who wants
it installs it and Joven finds it on ``PATH``.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import stat
import sys
import urllib.request
from pathlib import Path

KEPUBIFY_VERSION = "4.0.4"

KEPUBIFY_URL = "https://github.com/pgaskin/kepubify/releases/download/v{v}/{asset}"

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




def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendor",
        type=Path,
        default=Path(__file__).resolve().parent / "vendor",
        help="where to put them (default: packaging/vendor)",
    )
    args = parser.parse_args(argv)

    vendor: Path = args.vendor
    vendor.mkdir(parents=True, exist_ok=True)
    print(f"vendoring into {vendor}")

    print("kepubify:")
    fetch_kepubify(vendor)

    total = sum(f.stat().st_size for f in vendor.rglob("*") if f.is_file())
    print(f"\nvendor total: {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
