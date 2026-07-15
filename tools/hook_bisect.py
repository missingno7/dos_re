#!/usr/bin/env python3
"""hook_bisect — localize the first incorrect lifted/linked function by oracle.

The safety net of oracle-guided convergence (docs: the native-recovery pilot).
The pipeline integrates optimistically — lift+link the reachable graph without
per-function proof — and relies on END-TO-END oracle comparison to expose the
first incorrect transformation.  When the assembled hybrid (or native graph)
diverges from the pure interpreter, this tool binary-searches the installed
replacement set to name the SMALLEST responsible subset, so AI is invoked only
for that concrete gap instead of re-proving everything.

Method (deterministic, snapshot-anchored):
  1. Run a reference (no replacements) and the candidate (full set) for N
     boundaries from the same snapshot; find the first boundary whose masked
     state digest differs.  If none: the whole set is oracle-clean.
  2. Binary-search the installed set: install HALF, rerun to that boundary,
     compare.  Keep the half that still reproduces the first divergence.  The
     surviving singleton is the (or a) responsible function.
  3. Report its address + the divergence boundary; a suffix snapshot just
     before it is the repro (write with --repro-dir).

Game-agnostic: the caller supplies a `driver` object exposing
``fresh(install)`` -> runtime, ``advance(rt)`` (one boundary), and
``digest(rt)`` -> bytes.  ``install`` is the set of (cs,ip) keys to keep
(others are removed after the adapter installs its full set).

Usage (from a port root):
    python dos_re/tools/hook_bisect.py --driver lemmings.bisect_driver:Driver \
        --boundaries 60 [--repro-dir artifacts/bisect]
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def first_divergence(driver, keep, boundaries):
    """Return (index, ref_digest, cand_digest) of the first differing boundary,
    or None if the candidate set matches the reference over the whole run."""
    ref = driver.fresh(install=frozenset())          # pure oracle
    cand = driver.fresh(install=keep)                # candidate subset
    for i in range(boundaries):
        driver.advance(ref)
        driver.advance(cand)
        rd, cd = driver.digest(ref), driver.digest(cand)
        if rd != cd:
            return (i, rd, cd)
    return None


def bisect(driver, boundaries):
    """Binary-search the full installed set for a minimal responsible subset."""
    full = sorted(driver.all_keys())
    top = first_divergence(driver, frozenset(full), boundaries)
    if top is None:
        return None, [], full            # oracle-clean: no suspects, full set clean
    target_i = top[0]

    def reproduces(subset):
        d = first_divergence(driver, frozenset(subset), boundaries)
        return d is not None and d[0] <= target_i

    suspects = list(full)
    while len(suspects) > 1:
        mid = len(suspects) // 2
        lo, hi = suspects[:mid], suspects[mid:]
        if reproduces(lo):
            suspects = lo
        elif reproduces(hi):
            suspects = hi
        else:
            # Divergence needs an interaction across the split — keep the whole
            # set (report it; a pair/interaction bug, still localized to it).
            break
    return target_i, suspects, full


def _load(spec: str):
    mod, _, name = spec.partition(":")
    if not name:
        raise SystemExit(f"--driver needs MOD:CLASS, got {spec!r}")
    return getattr(importlib.import_module(mod), name)


def main(argv=None) -> int:
    import os
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--driver", required=True, metavar="MOD:CLASS")
    ap.add_argument("--boundaries", type=int, default=60)
    ap.add_argument("--repro-dir", default=None)
    args = ap.parse_args(argv)
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    driver = _load(args.driver)(repro_dir=args.repro_dir)

    at, suspects, full = bisect(driver, args.boundaries)
    if at is None:
        print(f"ORACLE-CLEAN: {len(full)} installed functions match the oracle "
              f"over {args.boundaries} boundaries.")
        return 0
    fmt = lambda k: "%04X:%04X" % k                  # noqa: E731
    print(f"FIRST DIVERGENCE at boundary {at}.")
    print(f"Responsible subset ({len(suspects)} of {len(full)}):")
    for k in suspects:
        print("  " + fmt(k))
    if hasattr(driver, "write_repro") and args.repro_dir:
        path = driver.write_repro(at, suspects)
        print(f"repro written: {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
