#!/usr/bin/env python3
"""Export one closed-world release plan.

The project factory is ``MODULE:CALLABLE`` and returns
``(release_plan, export_files, launcher_path)``.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.export import export_release  # noqa: E402


def _load(spec: str):
    module, separator, name = spec.partition(":")
    if not separator:
        raise SystemExit(f"--factory must be MODULE:CALLABLE, got {spec!r}")
    return getattr(importlib.import_module(module), name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory", required=True, metavar="MODULE:CALLABLE")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    plan, files, launcher = _load(args.factory)()
    manifest = export_release(plan, tuple(files), args.output, launcher=launcher)
    print(f"exported {len(manifest.files)} files")
    print(f"plan digest: {manifest.plan_digest}")
    print(f"artifact: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
