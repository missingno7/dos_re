#!/usr/bin/env python3
"""Regenerate a game adapter's recovered-island manifest from @oracle_link metadata.

Usage:
    python tools/gen_island_manifest.py <package> [<package> ...] -o docs/recovered_islands.md

e.g. (from an adapter repo vendoring this framework):
    python tools/gen_island_manifest.py mygame.codecs mygame.recovered -o docs/recovered_islands.md

Code is the source of truth: this tool *discovers* islands by importing the
given packages and reading the @oracle_link decorators; pair the committed
manifest with a drift test (generated == committed).

Origin: generalized from pre2_port's scripts/gen_island_manifest.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.islands import render_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", help="package(s) to scan for @oracle_link functions")
    parser.add_argument("-o", "--out", required=True, help="manifest markdown path to write")
    args = parser.parse_args(argv)

    manifest = render_manifest(tuple(args.packages))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(manifest, encoding="utf-8")
    rows = manifest.count("\n| `")
    print(f"wrote {out} ({rows} islands)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
