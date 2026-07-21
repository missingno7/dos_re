#!/usr/bin/env python3
"""Build, enrich, validate, and query a dos_re Execution Atlas."""
from __future__ import annotations

import argparse
import hashlib
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
        "metadata": dict(edge.metadata),
        "conflicts": {
            name: list(claims) for name, claims in edge.conflicts.items()
        },
    }


def _atlas(path: str) -> ExecutionAtlas:
    return ExecutionAtlas.open(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create an empty Atlas")
    create.add_argument("atlas")
    create.add_argument("--program", required=True)

    ingest_ir = commands.add_parser(
        "ingest-ir", help="import retained Recovery IR as one evidence source")
    ingest_ir.add_argument("atlas")
    ingest_ir.add_argument("--ir", required=True)
    ingest_ir.add_argument("--program", required=True)
    ingest_ir.add_argument("--image-label", required=True)
    ingest_ir.add_argument("--image-sha256", required=True)
    ingest_ir.add_argument("--address-space", default="real-mode")
    ingest_ir.add_argument("--root", action="append", default=[])
    ingest_ir.add_argument("--product-profile", action="append", default=[])

    ingest = commands.add_parser("ingest-replay", help="import oracle replay evidence")
    ingest.add_argument("atlas")
    ingest.add_argument("replay")
    ingest.add_argument("--json", action="store_true")

    facts = commands.add_parser(
        "ingest-facts", help="import explicit identity-based node/edge evidence")
    facts.add_argument("atlas")
    facts.add_argument("facts")
    facts.add_argument(
        "--identity",
        help="stable evidence-source identity (defaults to the JSON identity field)",
    )

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
    if args.command == "ingest-ir":
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
        report = _atlas(args.atlas).ingest_replay_with_report(args.replay)
        if args.json:
            _emit(report.to_json(), True)
        else:
            value = report.to_json()
            print(
                f"{report.replay_id}: "
                f"{value['visited_function_count']} visited functions, "
                f"{report.invocation_count} invocations, "
                f"{value['observed_edge_count']} observed edges / "
                f"{report.observation_count} transfers; "
                f"corpus delta +{len(report.new_node_ids)} nodes, "
                f"+{len(report.new_edges)} edges, "
                f"-{len(report.removed_node_ids)} nodes, "
                f"-{len(report.removed_edges)} edges"
            )
        return 0
    if args.command == "ingest-facts":
        document = json.loads(Path(args.facts).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise SystemExit("manual facts document must be a JSON object")
        identity = args.identity or document.get("identity")
        if not isinstance(identity, str) or not identity:
            raise SystemExit(
                "manual facts require --identity or a non-empty JSON identity field")
        nodes, edges = document.get("nodes", ()), document.get("edges", ())
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise SystemExit("manual facts nodes and edges must be JSON arrays")
        facts_path = Path(args.facts)
        print(_atlas(args.atlas).add_manual_facts(
            identity,
            provenance={
                "artifact": facts_path.name,
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            },
            nodes=nodes,
            edges=edges,
        ))
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
            "conflicts": {
                name: list(claims) for name, claims in node.conflicts.items()
            },
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
            "runtime_variants": list(item.runtime_variants),
            "incomplete": item.incomplete,
            "annotations": list(item.annotations),
        }, as_json)
    elif args.command == "unresolved":
        _emit([_edge(edge) for edge in atlas.unresolved()], as_json)
    elif args.command == "path":
        _emit(list(atlas.path(args.source, args.target)), as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
