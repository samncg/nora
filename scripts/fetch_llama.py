#!/usr/bin/env python
"""Download the llama.cpp `llama-server` binary for Windows into ./vendor/llama.

Picks the CUDA build when an NVIDIA GPU is present and the AVX2 CPU build
otherwise. Extract is flattened so llama-server.exe lands in vendor/llama/.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "llama"
RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "nora/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def has_nvidia() -> bool:
    for candidate in ("nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe"):
        exe = shutil.which(candidate) if "\\" not in candidate else candidate
        if not exe or not Path(exe).exists():
            continue
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if out.returncode == 0 and out.stdout.strip():
                print(f"gpu: {out.stdout.strip().splitlines()[0]}")
                return True
        except Exception:
            continue
    return False


def pick_assets(release: dict, cuda: bool) -> list[dict]:
    """Main binary plus matching cudart runtime, or the CPU build."""
    assets = release.get("assets", [])
    if cuda:
        for version in ("12.4", "13.4"):
            main = [
                a
                for a in assets
                if a["name"].startswith("llama-")
                and f"bin-win-cuda-{version}-x64" in a["name"]
                and a["name"].endswith(".zip")
            ]
            runtime = [
                a
                for a in assets
                if a["name"].startswith("cudart-llama-bin-win-cuda-")
                and f"cuda-{version}-x64" in a["name"]
                and a["name"].endswith(".zip")
            ]
            if main and runtime:
                return [main[0], runtime[0]]
    else:
        for pattern in ("bin-win-cpu-x64", "bin-win-avx2-x64"):
            cpu = [
                a
                for a in assets
                if a["name"].startswith("llama-")
                and pattern in a["name"]
                and a["name"].endswith(".zip")
            ]
            if cpu:
                return cpu[:1]
    return []


def download(url: str, target: Path) -> None:
    print(f"  fetching {target.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "nora/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1024 * 1024)


def extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def flatten(dest: Path) -> None:
    """Move files out of any single top-level directory the zip created."""
    for _ in range(2):
        entries = list(dest.iterdir())
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        if len(dirs) == 1 and not files:
            inner = dirs[0]
            for item in inner.iterdir():
                shutil.move(str(item), str(dest / item.name))
            inner.rmdir()
        else:
            break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true", help="force the CPU build")
    parser.add_argument("--force", action="store_true", help="redownload")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit(
            "this script installs the Windows build. on linux/macos install "
            "llama.cpp yourself and set server.binary in config.yaml."
        )

    exe = DEST / "llama-server.exe"
    if exe.exists() and not args.force:
        print(f"already installed: {exe}")
        return 0

    cuda = has_nvidia() and not args.cpu
    print("resolving llama.cpp release with windows builds ...")
    releases = get_json(RELEASES)

    chosen: tuple[dict, list[dict]] | None = None
    for release in releases if isinstance(releases, list) else []:
        picked = pick_assets(release, cuda)
        if picked:
            chosen = (release, picked)
            break

    if chosen is None:
        raise SystemExit(
            "no windows assets found in recent releases. download manually from "
            "https://github.com/ggml-org/llama.cpp/releases and extract "
            f"llama-server.exe + the cuda runtime dlls into {DEST}"
        )

    release, wanted = chosen
    print(f"release: {release.get('tag_name', '?')} ({'cuda' if cuda else 'cpu'})")

    DEST.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for asset in wanted:
            target = Path(tmp) / asset["name"]
            download(asset["browser_download_url"], target)
            print(f"  extracting {asset['name']}")
            extract(target, DEST)
        flatten(DEST)

    if not exe.exists():
        raise SystemExit(f"extracted, but {exe} is missing. check {DEST}")
    print(f"installed: {exe}")
    if cuda:
        print("cuda build: run nvidia-smi to confirm the driver sees the gpu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
