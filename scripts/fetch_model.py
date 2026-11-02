#!/usr/bin/env python
"""Find and download a 4-bit E4B Gemma GGUF into ./models.

Tries Gemma 4 E4B repos first, then falls back to Gemma 3n E4B. If the exact
repo you want is not in the list, pass --repo and --file explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://huggingface.co/api"
QUANTS = ["Q4_K_M", "Q4_K_S", "Q4_0", "IQ4_XS", "IQ4_NL", "Q4_1", "Q5_K_M"]
QUERIES = ["gemma-4-e4b", "gemma 4 e4b", "gemma-3n-e4b", "gemma e4b"]
TRUSTED = ("unsloth", "bartowski", "ggml-org", "lmstudio-community", "google")


def headers() -> dict[str, str]:
    h = {"User-Agent": "nora/1.0"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_json(url: str):
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)


def search_repos(limit: int = 40) -> list[dict]:
    seen: dict[str, dict] = {}
    for query in QUERIES:
        url = f"{API}/models?" + urllib.parse.urlencode(
            {"search": query, "limit": limit, "sort": "downloads", "direction": -1}
        )
        try:
            results = get_json(url)
        except Exception as exc:  # network or rate limit
            print(f"  ! search failed for {query!r}: {exc}")
            continue
        for repo in results:
            repo_id = repo.get("id", "")
            if "gguf" not in repo_id.lower():
                continue
            seen.setdefault(repo_id, repo)
    return list(seen.values())


def rank(repo: dict) -> tuple[int, int, int]:
    repo_id = repo.get("id", "").lower()
    trusted = 1 if any(t in repo_id for t in TRUSTED) else 0
    gated = 1 if repo.get("gated") else 0
    downloads = int(repo.get("downloads") or 0)
    return (-gated, trusted, downloads)


def list_files(repo_id: str) -> list[dict]:
    url = f"{API}/models/{repo_id}/tree/main?recursive=true"
    try:
        entries = get_json(url)
    except Exception as exc:
        print(f"  ! cannot list {repo_id}: {exc}")
        return []
    return [e for e in entries if e.get("type") == "file"]


def pick_mmproj(files: list[dict]) -> dict | None:
    """The vision projector: smallest sensible quant first."""
    candidates = [
        f
        for f in files
        if "mmproj" in str(f.get("path", "")).lower()
        and str(f.get("path", "")).lower().endswith(".gguf")
    ]
    if not candidates:
        return None
    for quant in ("q8_0", "f16", "bf16", "f32"):
        for entry in candidates:
            if quant in entry["path"].lower():
                return entry
    return candidates[0]


def fetch_mmproj(
    repo_id: str,
    outdir: Path,
    *,
    files: list[dict] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> Path | None:
    existing = sorted(outdir.glob("*mmproj*.gguf"))
    if existing and not force:
        print(f"vision projector already installed: {existing[0]}")
        return existing[0]

    entry = pick_mmproj(files if files is not None else list_files(repo_id))
    if entry is None:
        print(f"no mmproj file in {repo_id}; she will not be able to see images")
        return None

    size = int(entry.get("size") or 0)
    print(f"found: {repo_id}/{entry['path']}  ({human(size)})")
    dest = outdir / Path(entry["path"]).name
    if dry_run:
        print(f"would download {repo_id}/{entry['path']} -> {dest}")
        return None
    download(repo_id, entry["path"], dest)
    return dest


def pick_file(files: list[dict]) -> dict | None:
    names = [
        f
        for f in files
        if str(f.get("path", "")).lower().endswith(".gguf")
        and "mmproj" not in str(f.get("path", "")).lower()
        and "mtp" not in str(f.get("path", "")).lower()
    ]
    for quant in QUANTS:
        for entry in names:
            name = entry["path"].lower()
            if quant.lower() in name and "e2b" not in name:
                return entry
    return None


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def download(repo_id: str, filename: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo_id}/resolve/main/{urllib.parse.quote(filename)}?download=true"
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {repo_id}/{filename}")
    print(f"  -> {dest}")

    req = urllib.request.Request(url, headers=headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            next_tick = 0
            with open(part, "wb") as fh:
                while True:
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    fh.write(block)
                    done += len(block)
                    if total and done >= next_tick:
                        pct = done * 100 / total
                        sys.stdout.write(
                            f"\r  {pct:5.1f}%  {human(done)} / {human(total)}"
                        )
                        sys.stdout.flush()
                        next_tick = done + max(total // 20, 8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code in (401, 403):
            raise SystemExit(
                "this repo is gated. accept the license on huggingface.co and put "
                "your token in .env as HF_TOKEN, then rerun."
            )
        raise SystemExit(f"download failed: HTTP {exc.code}")
    print()
    part.replace(dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="explicit HF repo id")
    parser.add_argument("--file", help="explicit filename inside the repo")
    parser.add_argument("--dir", default=str(ROOT / "models"))
    parser.add_argument("--force", action="store_true", help="redownload if present")
    parser.add_argument(
        "--mmproj",
        action="store_true",
        help="also download the vision projector so she can see images",
    )
    parser.add_argument("--dry-run", action="store_true", help="only print what would be downloaded")
    args = parser.parse_args()

    outdir = Path(args.dir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.repo:
        files = list_files(args.repo)
        if args.file:
            entry = next((f for f in files if f["path"] == args.file), {"path": args.file})
        else:
            entry = pick_file(files)
            if not entry:
                raise SystemExit(f"no 4-bit gguf found in {args.repo}; pass --file")
        dest = outdir / Path(entry["path"]).name
        if dest.exists() and not args.force:
            print(f"already have {dest}")
            if args.mmproj:
                fetch_mmproj(
                    args.repo,
                    outdir,
                    files=files,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            return 0
        if args.dry_run:
            print(f"would download {args.repo}/{entry['path']} -> {dest}")
            if args.mmproj:
                fetch_mmproj(
                    args.repo,
                    outdir,
                    files=files,
                    force=args.force,
                    dry_run=True,
                )
            return 0
        download(args.repo, entry["path"], dest)
        if args.mmproj:
            fetch_mmproj(args.repo, outdir, files=files, force=args.force)
        print("done.")
        return 0

    print("searching huggingface for an E4B 4-bit gguf ...")
    repos = sorted(search_repos(), key=rank, reverse=True)
    if not repos:
        raise SystemExit(
            "no gguf repos found. download a GGUF manually into ./models and set "
            "model.path in config.yaml."
        )

    # First pass: only repos that actually ship Q4_K_M.
    listings: list[tuple[dict, dict]] = []
    for repo in repos[:15]:
        entry = pick_file(list_files(repo["id"]))
        if entry:
            listings.append((repo, entry))

    if not listings:
        raise SystemExit(
            "no suitable 4-bit file found. try:\n"
            "  python scripts/fetch_model.py --repo <org/repo> --file <name.gguf>"
        )

    # Prefer a K-quant, then a non-gated repo.
    def quality(pair: tuple[dict, dict]) -> tuple[int, int, int]:
        repo, entry = pair
        name = entry["path"].upper()
        k_quant = 1 if "Q4_K_M" in name else 0
        gated = 0 if repo.get("gated") else 1
        return (k_quant, gated, int(repo.get("downloads") or 0))

    repo, entry = max(listings, key=quality)
    repo_id = repo["id"]
    size = int(entry.get("size") or 0)
    print(f"found: {repo_id}/{entry['path']}  ({human(size)})")
    dest = outdir / Path(entry["path"]).name
    if dest.exists() and not args.force:
        print(f"already have {dest}")
        if args.mmproj:
            fetch_mmproj(
                repo_id, outdir, force=args.force, dry_run=args.dry_run
            )
        return 0
    if args.dry_run:
        print(f"would download {repo_id}/{entry['path']} -> {dest}")
        if args.mmproj:
            fetch_mmproj(repo_id, outdir, force=args.force, dry_run=True)
        return 0
    download(repo_id, entry["path"], dest)
    if args.mmproj:
        fetch_mmproj(repo_id, outdir, force=args.force)
    print("done. nora will pick this up automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
