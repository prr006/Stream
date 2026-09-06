#!/usr/bin/env python3
"""
vendor fetcher for the AC3 WASM audio POC (pure stdlib: no npm required).

Downloads pinned npm tarballs from registry.npmjs.org and extracts only the
files the browser needs into frontend/vendor/:

  frontend/vendor/mediabunny/dist/...          (MKV demuxer, ~0.5 MB)
  frontend/vendor/ffmpeg-ffmpeg/dist/...       (ffmpeg.wasm JS glue)
  frontend/vendor/ffmpeg-core/dist/esm/...     (ffmpeg-core.js + .wasm, ~31 MB)

Commit policy: frontend/vendor/ is gitignored (regenerable via this script).

Usage:  python backend/fetch_vendor.py [--force]
"""
import io
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BACKEND_DIR.parent / "frontend" / "vendor"

# npm package -> (version, vendor dir name, subpaths to keep)
PACKAGES = {
    "mediabunny":   ("1.55.7", "mediabunny", "dist/"),
    "@ffmpeg/ffmpeg": ("0.12.15", "ffmpeg-ffmpeg", "dist/"),
    "@ffmpeg/core":   ("0.12.10", "ffmpeg-core", "dist/"),
}

REGISTRY = "https://registry.npmjs.org"
UA = {"User-Agent": "stream-poc-vendor-fetcher/1.0"}


def tarball_url(pkg: str, version: str) -> str:
    base = pkg.split("/")[-1]  # '@ffmpeg/core' -> 'core'
    return f"{REGISTRY}/{pkg}/-/{base}-{version}.tgz"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    return data


def main() -> int:
    force = "--force" in sys.argv
    for pkg, (version, dest_name, keep_prefix) in PACKAGES.items():
        dest = VENDOR_DIR / dest_name
        if dest.exists() and not force:
            print(f"[skip] {pkg}@{version} already at {dest}")
            continue
        url = tarball_url(pkg, version)
        print(f"[fetch] {url}")
        tgz = fetch(url)
        if len(tgz) < 5_000:
            print(f"  !! suspiciously small tarball ({len(tgz)} bytes) — aborting")
            return 1
        tmp = io.BytesIO(tgz)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        kept = 0
        total = 0
        with tarfile.open(fileobj=tmp, mode="r:gz") as tf:
            for member in tf.getmembers():
                # npm tarballs are rooted at 'package/'
                rel = re.sub(r"^package/", "", member.name)
                if not rel.startswith(keep_prefix):
                    continue
                member.name = rel
                if member.isdir():
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                data = src.read()
                out.write_bytes(data)
                kept += 1
                total += len(data)
        print(f"  -> {dest_name}: {kept} files, {total/1048576:.1f} MB")

    # sanity: the exact files the frontend will request must exist
    required = [
        VENDOR_DIR / "mediabunny" / "dist",
        VENDOR_DIR / "ffmpeg-ffmpeg" / "dist" / "esm" / "index.js",
        VENDOR_DIR / "ffmpeg-core",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        print("MISSING after fetch:", *missing, sep="\n  ")
        return 1
    print("\nVendor tree ready under frontend/vendor/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
