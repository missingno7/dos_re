#!/usr/bin/env python3
"""Hash-audit and cold-start a closed-world dos_re release artifact."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.export import ExportError, verify_release_artifact  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="target runner and arguments; prefix with -- to end tool options",
    )
    args = parser.parse_args(argv)
    command = tuple(args.command)
    if command[:1] == ("--",):
        command = command[1:]
    try:
        completed = verify_release_artifact(args.artifact, command)
    except ExportError as exc:
        parser.error(str(exc))
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    print("release artifact hash audit and hermetic cold start passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
