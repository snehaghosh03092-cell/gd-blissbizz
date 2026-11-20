#!/usr/bin/env python3
"""Stage reviewed source, including uncommitted work; exclude secrets and machine runtimes."""

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOTS = ("backend", "frontend", "services", "libs", "scripts", "deploy", "docs")
SKIP = {
    ".git",
    ".venv",
    ".tools",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "uploads",
    "tls",
    "demo-private",
    "var",
    ".DS_Store",
}


def allowed(path):
    if path.is_symlink() or not path.is_file():
        return False
    parts = path.relative_to(ROOT).parts
    if any(part in SKIP or part.endswith(".egg-info") for part in parts):
        return False
    name = path.name
    if (name.startswith(".env") and not name.endswith(".example")) or name in {
        "release.env",
        "vps.env",
        "memory.md",
    }:
        return False
    return path.suffix.lower() not in {
        ".pem",
        ".key",
        ".p12",
        ".pyc",
        ".log",
        ".sql",
        ".db",
        ".sqlite3",
        ".gz",
    }


def create_bundle(release="20261120-platform-01"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", release):
        raise ValueError("Invalid release tag")
    files = [
        ROOT / name for name in ("README.md", "Makefile", ".dockerignore", ".gitignore")
    ]
    for name in ROOTS:
        files.extend(path for path in (ROOT / name).rglob("*") if allowed(path))
    files = sorted(set(files))
    manifest = {
        "release": release,
        "base_domain": "blissbiz.biz",
        "files": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    }
    output = ROOT / "var" / "releases"
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"blissbiz-next-{release}.tar.gz"
    with tarfile.open(target, "x:gz") as archive:
        for path in files:
            archive.add(
                path,
                arcname="blissbiz-next/" + str(path.relative_to(ROOT)),
                recursive=False,
            )
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("blissbiz-next/RELEASE-MANIFEST.json")
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(
        f"{checksum}  {target.name}\n"
    )
    print(
        f"Prepared {target}\n{len(files)} source files; no database data or runtime credentials.\nSHA256 {checksum}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="20261120-platform-01")
    create_bundle(parser.parse_args().release)
