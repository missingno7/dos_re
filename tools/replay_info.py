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
    capture = artifact.capture_profile()
    print(
        f"capture: {capture.profile_id} ({capture.role}) / "
        f"{capture.identity_digest}"
    )
    print(
        "oracle-backed timeline: "
        f"{'yes' if artifact.trusted else 'no'}"
    )
    print("profiles:")
    for profile, boundary_count in artifact.profiles():
        print(f"  {profile.profile_id}: {profile.role} / {profile.implementation} "
              f"({boundary_count} cached boundaries)")
    print(f"functions: {len(artifact._manifest['function_visits'])}")
    evidence = artifact.execution_evidence()
    if evidence is None:
        print("execution evidence: none")
    else:
        print(
            f"execution evidence: {len(evidence.transfers)} observed edges / "
            f"{sum(item.count for item in evidence.transfers)} transfers "
            f"({evidence.evidence_identity_digest})"
        )
    print(f"validations: {len(artifact.validations())}")
    print(f"annotated points: {len(artifact._manifest['points'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
