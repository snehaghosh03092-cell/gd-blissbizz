#!/usr/bin/env python3
"""Create NEW release secrets on the server; never read/copy development environment files."""

import argparse
import os
import re
import secrets
from pathlib import Path


def initialize(root: Path, tls_dir: Path, tag: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", tag):
        raise ValueError("Invalid release tag")
    tls_dir = tls_dir.resolve(strict=True)
    if any(c in str(tls_dir) for c in "\n\r$#'\"\\"):
        raise ValueError("Unsupported certificate directory characters")
    for name in ("fullchain.pem", "privkey.pem"):
        if not (tls_dir / name).is_file() or (tls_dir / name).stat().st_size == 0:
            raise ValueError(f"Missing or empty certificate file: {name}")
    target = root / "deploy" / "release.env"
    content = (
        f"RELEASE_TAG={tag}\nMYSQL_PASSWORD={secrets.token_hex(32)}\n"
        f"MYSQL_ROOT_PASSWORD={secrets.token_hex(32)}\n"
        f"INTERNAL_SIGNING_SECRET={secrets.token_hex(48)}\n"
        f'TLS_DIR="{tls_dir}"\nBIND_ADDRESS=127.0.0.1\nHTTP_PORT=18080\nHTTPS_PORT=18443\n'
        "ANTHROPIC_API_KEY=\nCHAT_MODEL=claude-haiku-5-5\nGUIDE_MODEL=claude-sonnet-5-5\n"
        "SWEEP_INTERVAL_SECONDS=0\n"
    )
    # O_EXCL refuses any existing file/symlink; reruns must not rotate DB passwords.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tls-dir", type=Path, required=True)
    parser.add_argument("--tag", default="20261120-platform-01")
    args = parser.parse_args()
    try:
        target = initialize(Path(__file__).resolve().parents[1], args.tls_dir, args.tag)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Created private {target}. Existing app and databases unchanged.")


if __name__ == "__main__":
    main()
