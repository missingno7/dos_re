#!/usr/bin/env python3
"""Run or bisect one dos_re 3.0 oracle/candidate replay interval.

The game adapter supplies ``MODULE:FACTORY``. The factory receives the
artifact path and returns ``(artifact, oracle_driver, candidate_driver)``.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dos_re.replay import ReplayPoint, bisect_divergence, verify_interval  # noqa: E402


def _load(spec: str):
    module, sep, name = spec.partition(":")
    if not sep:
        raise SystemExit(f"--driver must be MODULE:FACTORY, got {spec!r}")
    return getattr(importlib.import_module(module), name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("artifact")
    ap.add_argument("--driver", required=True, metavar="MODULE:FACTORY")
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--bisect", action="store_true")
    args = ap.parse_args(argv)
    artifact, oracle, candidate = _load(args.driver)(Path(args.artifact))
    start = ReplayPoint(args.start, artifact.timeline_id)
    end = ReplayPoint(args.end, artifact.timeline_id)
    if args.bisect:
        points = [ReplayPoint(i, artifact.timeline_id)
                  for i in range(args.start, args.end + 1)]
        found = bisect_divergence(artifact, oracle, candidate, points)
        if found is None:
            print(f"EQUIVALENT {args.start}..{args.end}")
            return 0
        before, after, result = found
        print(f"DIVERGENT transition {before.ordinal}->{after.ordinal}")
    else:
        result = verify_interval(artifact, oracle, candidate, start, end)
        if result.equivalent:
            print(f"EQUIVALENT {args.start}..{args.end} "
                  f"{result.comparison.oracle_digest}")
            return 0
        print(f"DIVERGENT {args.start}..{args.end}")
    for difference in result.comparison.differences:
        print("  " + difference)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
