#!/usr/bin/env python3
"""Inspect a dos_re 3.0 ReplayArtifact without restoring profile state."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dos_re.replay import ReplayArtifact  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("artifact")
    args = ap.parse_args(argv)
    artifact = ReplayArtifact.open(args.artifact)
    print(f"timeline: {artifact.timeline_id}")
    print(f"events: {len(artifact.events)} ({artifact.event_stream_sha256})")
    print("profiles:")
    for profile, boundary_count in artifact.profiles():
        print(f"  {profile.profile_id}: {profile.role} / {profile.implementation} "
              f"({boundary_count} cached boundaries)")
    print(f"functions: {len(artifact._manifest['function_visits'])}")
    print(f"annotated points: {len(artifact._manifest['points'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
