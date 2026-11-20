"""CLI: python -m app.seed [--reset] [--scale 1.0] [--anchor 2026-11-18] [--seed N] [--check] [--facts]

Seeds a realistic multi-tenant dataset into the configured DATABASE_URL (local databases only).
All accounts use the dev password printed at the end; e-mail addresses are on reserved example domains.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from app.seed import loader
from app.seed.reference import DEV_PASSWORD
from app.seed.verify import run_checks, violations
from app.seed.world import SEED_DEFAULT, build_world


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed BlissBiz development data")
    parser.add_argument(
        "--reset", action="store_true", help="empty all tables first (local database only)"
    )
    parser.add_argument(
        "--scale", type=float, default=1.0, help="activity multiplier (0.1 = quick small dataset)"
    )
    parser.add_argument(
        "--anchor",
        help="treat this UTC time as 'now' (YYYY-MM-DD or YYYY-MM-DDTHH:MM); default: now",
    )
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument(
        "--check", action="store_true", help="only run integrity checks on the current database"
    )
    parser.add_argument(
        "--facts", action="store_true", help="print ground-truth facts for the current database"
    )
    args = parser.parse_args()

    from app.db.session import engine

    if args.check or args.facts:
        if args.check:
            results = run_checks(engine)
            for name, n in results.items():
                print(
                    f"{'FAIL' if n else 'ok  '}  {name}" + (f"  ({n} offending rows)" if n else "")
                )
            sys.exit(1 if any(results.values()) else 0)
        from app.seed.facts import print_facts

        print_facts(engine)
        return

    anchor = (
        datetime.fromisoformat(args.anchor)
        if args.anchor
        else datetime.utcnow().replace(second=0, microsecond=0)
    )
    loader.assert_local(engine)
    if not loader.is_empty(engine):
        if not args.reset:
            sys.exit(
                "Data already present. Use --reset to empty the local database and seed again."
            )
    started = time.time()
    rows = build_world(anchor, args.scale, args.seed)
    print(f"simulated in {time.time() - started:.1f}s; inserting...")
    if args.reset:
        loader.reset(engine)
    counts = loader.load(engine, rows)
    bad = violations(engine)
    print(f"done in {time.time() - started:.1f}s")
    for table, n in sorted(counts.items()):
        print(f"  {table:32s} {n:>7}")
    print(f"\nanchor (UTC 'now'): {anchor.isoformat()}  scale={args.scale}  seed={args.seed}")
    print(f"dev password for every seeded account: {DEV_PASSWORD}")
    if bad:
        print("\nINTEGRITY VIOLATIONS:")
        for name, n in bad.items():
            print(f"  {name}: {n}")
        sys.exit(1)
    print(f"integrity checks: all {len(run_checks(engine))} passed")


if __name__ == "__main__":
    main()
