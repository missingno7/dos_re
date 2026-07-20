#!/usr/bin/env python3
"""Build, enrich, validate, and query a dos_re Execution Atlas."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.atlas import AtlasEdge, ExecutionAtlas  # noqa: E402
from dos_re.identity import ImageIdentity, ProgramIdentity  # noqa: E402


def _emit(value, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True, indent=2))
    elif isinstance(value, (list, tuple)):
        for item in value:
            print(item)
    else:
        print(value)


def _edge(edge: AtlasEdge) -> dict[str, object]:
    return {
        "source": edge.source, "target": edge.target, "kind": edge.kind,
        "status": edge.status, "observation_count": edge.observation_count,
        "evidence": list(edge.evidence),
    }


def _atlas(path: str) -> ExecutionAtlas:
    return ExecutionAtlas.open(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create an empty Atlas")
    create.add_argument("atlas")
    create.add_argument("--program", required=True)

    build = commands.add_parser("build", help="import retained Recovery IR")
    build.add_argument("atlas")
    build.add_argument("--ir", required=True)
    build.add_argument("--program", required=True)
    build.add_argument("--image-label", required=True)
    build.add_argument("--image-sha256", required=True)
    build.add_argument("--address-space", default="real-mode")
    build.add_argument("--root", action="append", default=[])
    build.add_argument("--product-profile", action="append", default=[])

    ingest = commands.add_parser("ingest-replay", help="import oracle replay evidence")
    ingest.add_argument("atlas")
    ingest.add_argument("replay")

    for name in ("validate", "show", "callers", "callees", "coverage",
                 "best-replay", "unresolved", "path"):
        command = commands.add_parser(name)
        command.add_argument("atlas")
        if name in {"show", "callers", "callees", "best-replay"}:
            command.add_argument("identity")
        elif name == "coverage":
            command.add_argument("product_profile")
        elif name == "path":
            command.add_argument("source")
            command.add_argument("target")
        command.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "create":
        atlas = ExecutionAtlas.create(args.atlas, program=ProgramIdentity(args.program))
        print(atlas.identity_digest)
        return 0
    if args.command == "build":
        path = Path(args.atlas)
        program = ProgramIdentity(args.program)
        atlas = (
            ExecutionAtlas.open(path) if (path / "manifest.json").exists()
            else ExecutionAtlas.create(path, program=program)
        )
        image = ImageIdentity(program, args.image_label, "sha256", args.image_sha256)
        atlas.import_recovery_ir(
            args.ir, image=image, address_space=args.address_space, roots=args.root)
        if args.root:
            for profile in args.product_profile or ["development"]:
                roots = [
                    node.identity for root in args.root
                    for node in (atlas.resolve(root),)
                ]
                atlas.set_product_roots(profile, roots)
        print(atlas.identity_digest)
        return 0
    if args.command == "ingest-replay":
        print(_atlas(args.atlas).ingest_replay(args.replay))
        return 0

    atlas = _atlas(args.atlas)
    as_json = bool(getattr(args, "json", False))
    if args.command == "validate":
        _emit({"valid": True, "identity_digest": atlas.identity_digest}, as_json)
    elif args.command == "show":
        node = atlas.resolve(args.identity)
        _emit({
            "identity": node.identity, "kind": node.kind, "label": node.label,
            "metadata": dict(node.metadata), "evidence": list(node.evidence),
        }, as_json)
    elif args.command == "callers":
        _emit([_edge(edge) for edge in atlas.callers(args.identity)], as_json)
    elif args.command == "callees":
        _emit([_edge(edge) for edge in atlas.callees(args.identity)], as_json)
    elif args.command == "coverage":
        coverage = atlas.coverage_for(args.product_profile)
        _emit({
            "roots": list(coverage.roots), "reachable": sorted(coverage.reachable),
            "unresolved_edges": list(coverage.unresolved_edges),
            "evidence_identity": coverage.evidence_identity,
        }, as_json)
    elif args.command == "best-replay":
        item = atlas.best_replay(args.identity)
        _emit({
            "replay_id": item.replay_id, "function_id": item.function_id,
            "invocation_count": item.invocation_count,
            "first_entry": (
                None if item.first_entry is None else item.first_entry.to_json()),
            "last_exit": None if item.last_exit is None else item.last_exit.to_json(),
            "cached_at_or_before_entry": (
                None if item.cached_at_or_before_entry is None
                else item.cached_at_or_before_entry.to_json()),
        }, as_json)
    elif args.command == "unresolved":
        _emit([_edge(edge) for edge in atlas.unresolved()], as_json)
    elif args.command == "path":
        _emit(list(atlas.path(args.source, args.target)), as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
