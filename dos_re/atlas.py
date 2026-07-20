"""Persistent query projection over evidence about a recovered program.

An Atlas can grow from any useful subset of retained Recovery IR, oracle
ReplayArtifact observations, runtime-code facts, or explicit manual facts.
Recovery IR is the strongest static skeleton when it exists, but it is not a
prerequisite: observed execution points and transfers can form a useful map
before function boundaries are known.

Source records remain independently attributable.  The Atlas stores normalized
copies beside deterministic derived indexes and implements
:class:`dos_re.execution.CoverageSource`; it does not become the decoder,
replay, implementation-selection, or runtime-dispatch authority.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .execution import ExecutionPlan, ImplementationCatalog, ProgramCoverage
from .identity import (
    BoundaryIdentity,
    ExecutionPointIdentity,
    FunctionIdentity,
    ImageIdentity,
    ProgramIdentity,
    RuntimeCodeSlotIdentity,
    RuntimeCodeVariantIdentity,
    real_mode_address,
)
from .lift.ir import load_recovery_ir
from .replay import (
    ReplayArtifact,
    ReplayError,
    ReplayExecutionIdentity,
    ReplayPoint,
)
from .runtime_code import RuntimeCodeSlot

ATLAS_FORMAT_VERSION = 1
MANIFEST = "manifest.json"


class AtlasError(RuntimeError):
    """Invalid, corrupt, ambiguous, or stale Atlas data."""


class StaleAtlasSourceError(AtlasError):
    """A retained evidence source changed since it was imported."""


@dataclass(frozen=True)
class AtlasNode:
    identity: str
    kind: str
    label: str
    metadata: Mapping[str, Any]
    evidence: tuple[str, ...]
    conflicts: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict)


@dataclass(frozen=True)
class AtlasEdge:
    source: str
    target: str
    kind: str
    status: str
    observation_count: int
    evidence: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    conflicts: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict)


@dataclass(frozen=True)
class ReplayCoverage:
    replay_id: str
    function_id: str
    invocation_count: int
    first_entry: ReplayPoint | None
    last_exit: ReplayPoint | None
    cached_at_or_before_entry: ReplayPoint | None
    cached_at_or_before_exit: ReplayPoint | None
    runtime_variants: tuple[str, ...] = ()
    incomplete: bool = False
    annotations: tuple[Mapping[str, Any], ...] = ()

    @property
    def complete(self) -> bool:
        return (
            not self.incomplete
            and self.first_entry is not None
            and self.last_exit is not None
        )


@dataclass(frozen=True)
class ReplayContribution:
    """Exact intrinsic and corpus-relative contribution of one replay."""

    replay_id: str
    artifact_label: str
    evidence_profile_identity_digest: str
    visited_function_ids: tuple[str, ...]
    invocation_count: int
    observed_edges: tuple[str, ...]
    observation_count: int
    new_node_ids: tuple[str, ...]
    new_edges: tuple[str, ...]
    removed_node_ids: tuple[str, ...] = ()
    removed_edges: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id,
            "artifact_label": self.artifact_label,
            "evidence_profile_identity_digest": (
                self.evidence_profile_identity_digest),
            "visited_function_ids": list(self.visited_function_ids),
            "visited_function_count": len(self.visited_function_ids),
            "invocation_count": self.invocation_count,
            "observed_edges": list(self.observed_edges),
            "observed_edge_count": len(self.observed_edges),
            "observation_count": self.observation_count,
            "new_node_ids": list(self.new_node_ids),
            "new_edges": list(self.new_edges),
            "removed_node_ids": list(self.removed_node_ids),
            "removed_edges": list(self.removed_edges),
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_digest(value: Mapping[str, Any]) -> str:
    clean = dict(value)
    clean.pop("source_digest", None)
    return _sha256(_canonical_json(clean))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AtlasError(f"cannot read Atlas JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AtlasError(f"Atlas JSON is not an object: {path}")
    return value


def _portable(value: Any) -> Any:
    """Strip machine-specific path prefixes from imported provenance."""
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_portable(item) for item in value]
    if isinstance(value, str):
        for path_type in (PurePosixPath, PureWindowsPath):
            candidate = path_type(value)
            if candidate.is_absolute():
                return candidate.name
    return value


def _point(raw: Mapping[str, Any] | None) -> ReplayPoint | None:
    return None if raw is None else ReplayPoint.from_json(raw)


def _claim(
    item: dict[str, Any], field_name: str, value: Any, source_id: str,
) -> None:
    """Retain one attributable metadata/label claim for materialization."""
    claim = {"source": source_id, "value": _portable(value)}
    claims = item.setdefault("_claims", {}).setdefault(field_name, [])
    if claim not in claims:
        claims.append(claim)


def _materialize_claims(item: dict[str, Any]) -> None:
    """Expose disagreements instead of silently choosing a source's metadata."""
    conflicts: dict[str, list[dict[str, Any]]] = {}
    for field_name, claims in sorted(item.pop("_claims", {}).items()):
        ordered = sorted(
            claims,
            key=lambda claim: (
                str(claim["source"]), _canonical_json(claim["value"])),
        )
        distinct = {
            _canonical_json(claim["value"]): claim["value"] for claim in ordered
        }
        if len(distinct) == 1:
            value = next(iter(distinct.values()))
            if field_name == "label":
                item["label"] = value
            else:
                item["metadata"][field_name] = value
            continue
        conflicts[field_name] = ordered
        if field_name == "label":
            item["label"] = ordered[0]["value"]
        else:
            item["metadata"].pop(field_name, None)
    item["conflicts"] = conflicts


class ExecutionAtlas:
    """Persistent normalized sources plus deterministic query indexes."""

    def __init__(self, directory: Path, manifest: dict[str, Any]):
        self.directory = Path(directory)
        self._manifest = manifest
        self._graph: dict[str, Any] | None = None
        self._replays: dict[str, Any] | None = None

    @classmethod
    def create(
        cls, directory: str | Path, *, program: ProgramIdentity,
        product_roots: Mapping[str, Sequence[str]] | None = None,
    ) -> "ExecutionAtlas":
        directory = Path(directory)
        path = directory / MANIFEST
        if path.exists():
            raise FileExistsError(path)
        manifest = {
            "format_version": ATLAS_FORMAT_VERSION,
            "program_id": str(program),
            "product_roots": {
                str(profile): sorted(set(map(str, roots)))
                for profile, roots in sorted((product_roots or {}).items())
            },
            "sources": [],
            "index_digest": "",
        }
        atlas = cls(directory, manifest)
        _write_json(path, manifest)
        atlas.rematerialize()
        return atlas

    @classmethod
    def open(cls, directory: str | Path, *, validate: bool = True) -> "ExecutionAtlas":
        directory = Path(directory)
        manifest = _read_json(directory / MANIFEST)
        if int(manifest.get("format_version", 0)) != ATLAS_FORMAT_VERSION:
            raise AtlasError("unsupported Execution Atlas format")
        atlas = cls(directory, manifest)
        if validate:
            atlas.validate()
        return atlas

    @property
    def program(self) -> ProgramIdentity:
        return ProgramIdentity(str(self._manifest["program_id"]))

    @property
    def identity_digest(self) -> str:
        return _sha256(_canonical_json({
            "format_version": ATLAS_FORMAT_VERSION,
            "program_id": str(self.program),
            "product_roots": self._manifest["product_roots"],
            "sources": [
                {"kind": item["kind"], "identity": item["identity"],
                 "source_digest": item["source_digest"]}
                for item in self._manifest["sources"]
            ],
        }))

    def set_product_roots(self, product_profile: str, roots: Iterable[str]) -> None:
        roots = sorted(set(map(str, roots)))
        if not product_profile or not roots:
            raise ValueError("product profile and at least one root are required")
        self._manifest["product_roots"][str(product_profile)] = roots
        self.rematerialize()

    def import_recovery_ir(
        self, ir_path: str | Path, *, image: ImageIdentity,
        address_space: str = "real-mode", roots: Iterable[str] = (),
        runtime_code: Iterable[RuntimeCodeSlot] = (),
    ) -> str:
        """Import retained IR records without re-decoding their bytes."""
        if image.program != self.program:
            raise ValueError("Recovery IR image belongs to another program")
        ir_path = Path(ir_path)
        document = load_recovery_ir(ir_path)
        ir_digest = _sha256(ir_path.read_bytes())
        function_records = document.get("functions", {})
        records = (
            function_records.values()
            if isinstance(function_records, Mapping) else function_records
        )
        entries = {
            str(record["entry"]).upper(): record
            for record in records
        }
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        source_id = f"recovery-ir:{image}"
        nodes[str(self.program)] = {
            "id": str(self.program), "kind": "program", "label": str(self.program),
            "metadata": {}, "evidence": [source_id],
        }
        nodes[str(image)] = {
            "id": str(image), "kind": "image", "label": image.label,
            "metadata": {
                "hash_algorithm": image.hash_algorithm,
                "content_digest": image.content_digest,
                "address_space": address_space,
            },
            "evidence": [source_id],
        }
        edges.append({
            "source": str(self.program), "target": str(image),
            "kind": "contains", "status": "containment",
            "observation_count": 0, "metadata": {}, "evidence": [source_id],
        })

        def function_id(entry: str) -> str:
            cs, ip = (int(part, 16) for part in entry.split(":"))
            return str(FunctionIdentity(
                image, address_space, real_mode_address(cs, ip)))

        def point_id(cs: int, ip: int) -> str:
            return str(ExecutionPointIdentity(
                image, address_space, real_mode_address(cs, ip)))

        for entry, record in sorted(entries.items()):
            identity = function_id(entry)
            refusals = tuple(sorted(
                (str(item.get("reason", "")), str(item.get("detail", "")))
                for item in record.get("refusals", ())
            ))
            nodes[identity] = {
                "id": identity,
                "kind": "function",
                "label": str(record.get("symbol") or entry),
                "metadata": {
                    "image_id": str(image),
                    "address_space": address_space,
                    "entry": entry,
                    "liftable": bool(record.get("liftable")),
                    "refusals": [
                        {"reason": reason, "detail": detail}
                        for reason, detail in refusals
                    ],
                    "exits": sorted(map(str, record.get("exits", ()))),
                    "signature": str(record.get("signature", "")),
                    "smc": _portable(record.get("smc")),
                },
                "evidence": [source_id],
            }
            edges.append({
                "source": str(image), "target": identity,
                "kind": "contains", "status": "containment",
                "observation_count": 0, "metadata": {}, "evidence": [source_id],
            })

        def add_transfer(
            source: str, target_entry: str, kind: str, *,
            detail: str = "", target_cs: int | None = None,
        ) -> None:
            source_cs = int(entries[target_source]["entry"].split(":")[0], 16)
            if target_cs is None:
                target_cs = source_cs
            normalized = f"{target_cs:04X}:{int(target_entry, 16):04X}"
            if normalized in entries:
                target = function_id(normalized)
                status = "resolved"
            else:
                target = point_id(target_cs, int(target_entry, 16))
                status = "frontier"
                nodes.setdefault(target, {
                    "id": target, "kind": "execution-point",
                    "label": normalized,
                    "metadata": {
                        "image_id": str(image), "address_space": address_space,
                        "address": normalized, "frontier_reason": detail or kind,
                    },
                    "evidence": [source_id],
                })
            edges.append({
                "source": source, "target": target, "kind": kind,
                "status": status, "observation_count": 0,
                "metadata": {"detail": detail} if detail else {},
                "evidence": [source_id],
            })

        for target_source, record in sorted(entries.items()):
            source = function_id(target_source)
            for target in record.get("calls_near", ()):
                add_transfer(source, str(target), "call")
            for target in record.get("calls_far", ()):
                segment, offset = int(target[0], 16), str(target[1])
                add_transfer(source, offset, "call-far", target_cs=segment)
            for interrupt in record.get("ints", ()):
                boundary = str(BoundaryIdentity(
                    self.program, "interrupt", f"{int(interrupt, 16):02x}"))
                nodes.setdefault(boundary, {
                    "id": boundary, "kind": "boundary",
                    "label": f"INT {int(interrupt, 16):02X}",
                    "metadata": {"namespace": "interrupt", "number": str(interrupt)},
                    "evidence": [source_id],
                })
                edges.append({
                    "source": source, "target": boundary, "kind": "interrupt",
                    "status": "boundary", "observation_count": 0,
                    "metadata": {}, "evidence": [source_id],
                })

            known_cross_targets: set[str] = set()
            for block in record.get("blocks", ()):
                for instruction in block.get("instructions", ()):
                    kind = str(instruction.get("kind", ""))
                    if instruction.get("dispatch_entry") or instruction.get("boundary_effect"):
                        site = point_id(
                            int(target_source.split(":")[0], 16),
                            int(str(instruction["ip"]), 16))
                        nodes.setdefault(site, {
                            "id": site, "kind": "execution-point",
                            "label": f"{target_source.split(':')[0]}:{instruction['ip']}",
                            "metadata": {
                                "image_id": str(image), "address_space": address_space,
                                "address": (
                                    f"{target_source.split(':')[0]}:{instruction['ip']}"),
                            },
                            "evidence": [source_id],
                        })
                        nodes[site]["metadata"].update({
                            "dispatch_entry": bool(instruction.get("dispatch_entry")),
                            "boundary_effect": bool(instruction.get("boundary_effect")),
                        })
                        edges.append({
                            "source": source, "target": site,
                            "kind": "contains", "status": "containment",
                            "observation_count": 0, "metadata": {},
                            "evidence": [source_id],
                        })
                    if kind in {"call_ind", "jmp_ind"}:
                        site = point_id(
                            int(target_source.split(":")[0], 16),
                            int(str(instruction["ip"]), 16))
                        nodes.setdefault(site, {
                            "id": site, "kind": "execution-point",
                            "label": f"{target_source.split(':')[0]}:{instruction['ip']}",
                            "metadata": {
                                "image_id": str(image), "address_space": address_space,
                                "address": (
                                    f"{target_source.split(':')[0]}:{instruction['ip']}"),
                                "frontier_reason": kind,
                            },
                            "evidence": [source_id],
                        })
                        edges.append({
                            "source": source, "target": site, "kind": kind,
                            "status": "unresolved", "observation_count": 0,
                            "metadata": {"instruction": instruction["ip"]},
                            "evidence": [source_id],
                        })
                        edges.append({
                            "source": source, "target": site,
                            "kind": "contains", "status": "containment",
                            "observation_count": 0, "metadata": {},
                            "evidence": [source_id],
                        })
                    target = instruction.get("target")
                    if kind in {"jmp", "jcc"} and target is not None:
                        candidate = (
                            f"{target_source.split(':')[0]}:{int(str(target), 16):04X}")
                        if candidate in entries and candidate != target_source \
                                and candidate not in known_cross_targets:
                            add_transfer(source, str(target), "tail-transfer")
                            known_cross_targets.add(candidate)
                    effect = instruction.get("platform_effect")
                    if effect:
                        boundary = str(BoundaryIdentity(
                            self.program, "platform-effect", str(effect)))
                        nodes.setdefault(boundary, {
                            "id": boundary, "kind": "boundary", "label": str(effect),
                            "metadata": {
                                "namespace": "platform-effect", "effect": str(effect)},
                            "evidence": [source_id],
                        })
                        edges.append({
                            "source": source, "target": boundary,
                            "kind": "platform-effect", "status": "boundary",
                            "observation_count": 0,
                            "metadata": {"instruction": instruction["ip"]},
                            "evidence": [source_id],
                        })

        for slot in sorted(runtime_code, key=lambda item: item.addr):
            address = real_mode_address(*slot.addr)
            slot_identity = RuntimeCodeSlotIdentity(image, address_space, address)
            nodes[str(slot_identity)] = {
                "id": str(slot_identity), "kind": "runtime-code-slot",
                "label": slot.name,
                "metadata": {
                    "image_id": str(image), "address_space": address_space,
                    "address": address, "subsystem": slot.subsystem,
                    "role": slot.role,
                    "writer_status": slot.writer_status,
                    "staticized": slot.is_staticized,
                },
                "evidence": [source_id],
            }
            for variant in sorted(slot.variants, key=lambda item: item.sha1):
                variant_id = str(RuntimeCodeVariantIdentity(slot_identity, "sha1", variant.sha1))
                nodes[variant_id] = {
                    "id": variant_id, "kind": "runtime-code-variant",
                    "label": variant.name,
                    "metadata": {
                        "slot_id": str(slot_identity), "size": variant.size,
                        "status": variant.status, "observed_in": list(variant.observed_in),
                    },
                    "evidence": [source_id],
                }
                edges.append({
                    "source": str(slot_identity), "target": variant_id,
                    "kind": "has-variant", "status": "resolved",
                    "observation_count": 0, "metadata": {}, "evidence": [source_id],
                })

        root_ids = [function_id(root.upper()) if root.upper() in entries else str(root)
                    for root in roots]
        source = {
            "format_version": ATLAS_FORMAT_VERSION,
            "kind": "static",
            "identity": source_id,
            "program_id": str(self.program),
            "image": {
                "identity": str(image), "label": image.label,
                "hash_algorithm": image.hash_algorithm,
                "content_digest": image.content_digest,
                "address_space": address_space,
            },
            "recovery_ir": {
                "version": int(document["ir_version"]), "sha256": ir_digest,
                "provenance": _portable(document.get("provenance", {})),
                "facts_applied": _portable(document.get("facts_applied", [])),
            },
            "roots": sorted(set(root_ids)),
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": sorted(edges, key=lambda edge: (
                edge["source"], edge["target"], edge["kind"],
                edge["status"], _canonical_json(edge["metadata"]),
            )),
        }
        return self._store_source(source)

    def ingest_replay(self, artifact_path: str | Path) -> str:
        """Import trusted oracle-owned visits and observed transfers."""
        return self.ingest_replay_with_report(artifact_path).replay_id

    def ingest_replay_with_report(
        self, artifact_path: str | Path,
    ) -> ReplayContribution:
        """Import a replay and report its exact corpus delta."""
        artifact = ReplayArtifact.open(artifact_path)
        if not artifact.trusted:
            raise AtlasError(
                "Atlas replay evidence requires a complete equivalent "
                "oracle/capture validation"
            )
        evidence = artifact.execution_evidence()
        if evidence is None:
            raise AtlasError(
                "Atlas replay evidence requires post-hoc oracle execution "
                "evidence"
            )
        try:
            profile = artifact.profile_by_digest(
                evidence.profile_identity_digest)
        except ReplayError as exc:
            raise AtlasError(
                "replay execution evidence names an absent profile"
            ) from exc
        if profile.role != "oracle":
            raise AtlasError(
                "Atlas execution evidence must come from an oracle profile"
            )
        if evidence.profile_identity_digest != profile.identity_digest:
            raise StaleAtlasSourceError("replay execution evidence has a stale oracle identity")
        cached = artifact.cached_points(profile)

        def nearest(point: ReplayPoint | None) -> ReplayPoint | None:
            if point is None:
                return None
            choices = [item for item in cached if item.ordinal <= point.ordinal]
            return max(choices, key=lambda item: item.ordinal) if choices else None

        annotations = artifact.point_annotations()

        def interval_annotations(
            first: ReplayPoint | None, last: ReplayPoint | None,
        ) -> list[dict[str, Any]]:
            if first is None:
                return []
            return [
                annotation for annotation in annotations
                if ReplayPoint.from_json(annotation["point"]).ordinal
                >= first.ordinal
                and (
                    last is None
                    or ReplayPoint.from_json(annotation["point"]).ordinal
                    <= last.ordinal
                )
            ]

        visits = []
        for visit in artifact.function_visits():
            visits.append({
                "function_id": visit.function_id,
                "invocation_count": visit.invocation_count,
                "first_entry": (
                    None if visit.first_entry is None else visit.first_entry.to_json()),
                "last_exit": None if visit.last_exit is None else visit.last_exit.to_json(),
                "cached_at_or_before_entry": (
                    None if nearest(visit.first_entry) is None
                    else nearest(visit.first_entry).to_json()),
                "cached_at_or_before_exit": (
                    None if nearest(visit.last_exit) is None
                    else nearest(visit.last_exit).to_json()),
                "annotations": interval_annotations(
                    visit.first_entry, visit.last_exit),
                "incomplete": visit.incomplete,
            })
        replay_id = f"replay:{artifact.identity_digest}"
        capture = artifact.capture_profile()
        source = {
            "format_version": ATLAS_FORMAT_VERSION,
            "kind": "replay",
            "identity": replay_id,
            "program_id": str(self.program),
            "artifact": {
                "label": Path(artifact_path).name,
                "identity_digest": artifact.identity_digest,
                "timeline_id": artifact.timeline_id,
                "event_stream_sha256": artifact.event_stream_sha256,
                "capture_profile": capture.to_json(),
                "capture_profile_identity_digest": capture.identity_digest,
                "oracle_profile": profile.to_json(),
                "oracle_profile_identity_digest": profile.identity_digest,
                "validation_count": len(artifact.validations()),
            },
            "visits": sorted(visits, key=lambda item: item["function_id"]),
            "transfers": (
                [] if evidence is None
                else [item.to_json() for item in evidence.transfers]),
            "runtime_variants": (
                [] if evidence is None else list(evidence.runtime_variants)),
            "incomplete_functions": (
                list(evidence.incomplete_functions)),
            "evidence_provenance": evidence.provenance,
            "evidence_identity_digest": evidence.evidence_identity_digest,
            "annotations": list(annotations),
        }
        before_nodes = {node.identity for node in self.nodes()}
        before_edges = {
            self._edge_report_identity(edge) for edge in self.edges()
        }
        self._store_source(source)
        after_nodes = {node.identity for node in self.nodes()}
        after_edges = {
            self._edge_report_identity(edge) for edge in self.edges()
        }
        visits_by_id = {
            visit.function_id: visit for visit in artifact.function_visits()
        }
        observed_edges = tuple(sorted(
            f"{item.source_id} --{item.kind}--> {item.target_id} "
            f"(count={item.count})"
            for item in evidence.transfers
        ))
        return ReplayContribution(
            replay_id=replay_id,
            artifact_label=Path(artifact_path).name,
            evidence_profile_identity_digest=profile.identity_digest,
            visited_function_ids=tuple(sorted(visits_by_id)),
            invocation_count=sum(
                visit.invocation_count for visit in visits_by_id.values()),
            observed_edges=observed_edges,
            observation_count=sum(item.count for item in evidence.transfers),
            new_node_ids=tuple(sorted(after_nodes - before_nodes)),
            new_edges=tuple(sorted(after_edges - before_edges)),
            removed_node_ids=tuple(sorted(before_nodes - after_nodes)),
            removed_edges=tuple(sorted(before_edges - after_edges)),
        )

    @staticmethod
    def _edge_report_identity(edge: AtlasEdge) -> str:
        return (
            f"{edge.source} --{edge.kind}/{edge.status}--> {edge.target}"
        )

    def add_manual_facts(
        self, identity: str, *, provenance: Mapping[str, Any],
        nodes: Iterable[Mapping[str, Any]] = (),
        edges: Iterable[Mapping[str, Any]] = (),
    ) -> str:
        """Add explicit recovered facts with their own cited source identity."""
        if not identity:
            raise ValueError("manual evidence identity must not be empty")
        if not provenance:
            raise ValueError("manual evidence requires source provenance")
        source = {
            "format_version": ATLAS_FORMAT_VERSION,
            "kind": "manual",
            "identity": f"manual:{identity}",
            "program_id": str(self.program),
            "provenance": _portable(dict(provenance)),
            "nodes": [dict(item) for item in nodes],
            "edges": [dict(item) for item in edges],
        }
        return self._store_source(source)

    def _store_source(self, source: dict[str, Any]) -> str:
        if source["program_id"] != str(self.program):
            raise ValueError("Atlas source belongs to another program")
        source["source_digest"] = _source_digest(source)
        filename = (
            f"{source['kind']}-{_sha256(str(source['identity']).encode())[:20]}.json")
        relative = Path("sources") / filename
        entry = {
            "kind": source["kind"], "identity": source["identity"],
            "path": relative.as_posix(), "source_digest": source["source_digest"],
        }
        existing = next((
            item for item in self._manifest["sources"]
            if item["kind"] == entry["kind"]
            and item["identity"] == entry["identity"]
        ), None)
        if (
            existing is not None
            and existing.get("source_digest") == entry["source_digest"]
            and (self.directory / existing["path"]).is_file()
        ):
            return str(source["identity"])
        _write_json(self.directory / relative, source)
        current = [
            item for item in self._manifest["sources"]
            if not (item["kind"] == entry["kind"] and item["identity"] == entry["identity"])
        ]
        current.append(entry)
        self._manifest["sources"] = sorted(
            current, key=lambda item: (item["kind"], item["identity"]))
        self.rematerialize()
        return str(source["identity"])

    def rematerialize(self) -> None:
        nodes: dict[str, dict[str, Any]] = {}
        edge_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        replay_rows: list[dict[str, Any]] = []
        replay_sources: list[dict[str, Any]] = []
        for entry, source in self._sources():
            source_id = str(entry["identity"])
            if source["kind"] in {"static", "manual"}:
                for raw in source.get("nodes", ()):
                    identity = str(raw["id"])
                    node = nodes.setdefault(identity, {
                        "id": identity, "kind": str(raw["kind"]),
                        "label": str(raw.get("label", identity)),
                        "metadata": {}, "evidence": [],
                    })
                    if node["kind"] != str(raw["kind"]):
                        raise AtlasError(f"conflicting node kinds for {identity}")
                    if node["metadata"].get("observed_only"):
                        node["metadata"].pop("observed_only", None)
                    _claim(
                        node, "label", str(raw.get("label", identity)), source_id)
                    for name, value in raw.get("metadata", {}).items():
                        _claim(node, str(name), value, source_id)
                    node["evidence"] = sorted(set(node["evidence"]) | {source_id})
                for raw in source.get("edges", ()):
                    key = (
                        str(raw["source"]), str(raw["target"]), str(raw["kind"]),
                        str(raw.get("status", "resolved")),
                    )
                    edge = edge_map.setdefault(key, {
                        "source": key[0], "target": key[1], "kind": key[2],
                        "status": key[3], "observation_count": 0,
                        "metadata": {}, "evidence": [],
                    })
                    for name, value in raw.get("metadata", {}).items():
                        _claim(edge, str(name), value, source_id)
                    edge["evidence"] = sorted(set(edge["evidence"]) | {source_id})
            elif source["kind"] == "replay":
                runtime_variants = tuple(sorted(map(
                    str, source.get("runtime_variants", ()))))
                incomplete_functions = frozenset(map(
                    str, source.get("incomplete_functions", ())))
                annotations = tuple(source.get("annotations", ()))
                replay_sources.append({
                    "replay_id": source_id,
                    "artifact": source["artifact"],
                    "runtime_variants": list(runtime_variants),
                    "incomplete_functions": sorted(incomplete_functions),
                    "annotations": list(annotations),
                })
                for variant_id in runtime_variants:
                    variant = nodes.setdefault(variant_id, {
                        "id": variant_id,
                        "kind": "runtime-code-variant",
                        "label": variant_id,
                        "metadata": {"observed_only": True},
                        "evidence": [],
                    })
                    variant["evidence"] = sorted(
                        set(variant["evidence"]) | {source_id})
                for visit in source.get("visits", ()):
                    function_id = str(visit["function_id"])
                    nodes.setdefault(function_id, {
                        "id": function_id, "kind": "function",
                        "label": function_id, "metadata": {"observed_only": True},
                        "evidence": [],
                    })
                    nodes[function_id]["evidence"] = sorted(
                        set(nodes[function_id]["evidence"]) | {source_id})
                    replay_rows.append({
                        "replay_id": source_id, **visit,
                        "artifact": source["artifact"],
                        "runtime_variants": list(runtime_variants),
                        "incomplete": (
                            bool(visit.get("incomplete", False))
                            or function_id in incomplete_functions
                        ),
                    })
                for raw in source.get("transfers", ()):
                    key = (
                        str(raw["source_id"]), str(raw["target_id"]),
                        str(raw["kind"]), "observed",
                    )
                    edge = edge_map.setdefault(key, {
                        "source": key[0], "target": key[1], "kind": key[2],
                        "status": "observed", "observation_count": 0,
                        "metadata": {}, "evidence": [],
                    })
                    edge["observation_count"] += int(raw["count"])
                    edge["evidence"] = sorted(set(edge["evidence"]) | {source_id})

        for edge in edge_map.values():
            for endpoint in (edge["source"], edge["target"]):
                nodes.setdefault(endpoint, {
                    "id": endpoint, "kind": "execution-point",
                    "label": endpoint, "metadata": {"observed_only": True},
                    "evidence": list(edge["evidence"]),
                })
        for node in nodes.values():
            _materialize_claims(node)
        for edge in edge_map.values():
            _materialize_claims(edge)
        graph = {
            "format_version": ATLAS_FORMAT_VERSION,
            "program_id": str(self.program),
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": sorted(edge_map.values(), key=lambda item: (
                item["source"], item["target"], item["kind"], item["status"])),
        }
        replay_index = {
            "format_version": ATLAS_FORMAT_VERSION,
            "program_id": str(self.program),
            "replays": sorted(
                replay_sources, key=lambda item: item["replay_id"]),
            "coverage": sorted(replay_rows, key=lambda item: (
                item["function_id"], item["replay_id"])),
        }
        _write_json(self.directory / "indexes" / "graph.json", graph)
        _write_json(self.directory / "indexes" / "replay_coverage.json", replay_index)
        self._manifest["index_digest"] = _sha256(
            _canonical_json({"graph": graph, "replays": replay_index}))
        _write_json(self.directory / MANIFEST, self._manifest)
        self._graph, self._replays = graph, replay_index

    def validate(self) -> None:
        sources = list(self._sources())
        for entry, source in sources:
            if source.get("source_digest") != entry.get("source_digest") \
                    or _source_digest(source) != entry.get("source_digest"):
                raise StaleAtlasSourceError(
                    f"Atlas source changed: {entry.get('identity')}")
            if source.get("program_id") != str(self.program):
                raise AtlasError(f"Atlas source belongs to another program: {entry['identity']}")
        graph = _read_json(self.directory / "indexes" / "graph.json")
        replays = _read_json(self.directory / "indexes" / "replay_coverage.json")
        expected = _sha256(_canonical_json({"graph": graph, "replays": replays}))
        if expected != self._manifest.get("index_digest"):
            raise StaleAtlasSourceError(
                "derived Atlas indexes are stale; run atlas build/rematerialize")
        node_ids = [str(item["id"]) for item in graph.get("nodes", ())]
        if len(node_ids) != len(set(node_ids)):
            raise AtlasError("duplicate Atlas node identity")
        self._graph, self._replays = graph, replays

    def _sources(self):
        for entry in self._manifest["sources"]:
            path = (self.directory / entry["path"]).resolve()
            try:
                path.relative_to(self.directory.resolve())
            except ValueError as exc:
                raise AtlasError("Atlas source path escapes its directory") from exc
            yield entry, _read_json(path)

    def _indexes(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._graph is None or self._replays is None:
            self.validate()
        assert self._graph is not None and self._replays is not None
        return self._graph, self._replays

    def nodes(self, *, kind: str | None = None) -> tuple[AtlasNode, ...]:
        graph, _ = self._indexes()
        return tuple(
            AtlasNode(
                str(item["id"]), str(item["kind"]), str(item["label"]),
                dict(item.get("metadata", {})), tuple(item.get("evidence", ())),
                {
                    str(name): tuple(dict(claim) for claim in claims)
                    for name, claims in item.get("conflicts", {}).items()
                })
            for item in graph["nodes"]
            if kind is None or item["kind"] == kind
        )

    def resolve(self, query: str) -> AtlasNode:
        exact = [node for node in self.nodes() if node.identity == query]
        if exact:
            return exact[0]
        folded = query.casefold()
        labels = [node for node in self.nodes() if node.label.casefold() == folded]
        functions = [node for node in labels if node.kind == "function"]
        if len(functions) == 1:
            return functions[0]
        if len(labels) == 1:
            return labels[0]
        if len(labels) > 1:
            raise AtlasError(
                f"ambiguous Atlas query {query!r}: "
                + ", ".join(node.identity for node in labels[:8]))
        matches = [
            node for node in self.nodes()
            if folded in node.identity.casefold() or folded in node.label.casefold()
        ]
        if not matches:
            raise KeyError(query)
        if len(matches) != 1:
            raise AtlasError(
                f"ambiguous Atlas query {query!r}: "
                + ", ".join(node.identity for node in matches[:8]))
        return matches[0]

    def edges(self) -> tuple[AtlasEdge, ...]:
        graph, _ = self._indexes()
        return tuple(
            AtlasEdge(
                str(item["source"]), str(item["target"]), str(item["kind"]),
                str(item["status"]), int(item.get("observation_count", 0)),
                tuple(item.get("evidence", ())),
                dict(item.get("metadata", {})),
                {
                    str(name): tuple(dict(claim) for claim in claims)
                    for name, claims in item.get("conflicts", {}).items()
                })
            for item in graph["edges"]
        )

    def callers(self, identity: str) -> tuple[AtlasEdge, ...]:
        identity = self.resolve(identity).identity
        return tuple(
            edge for edge in self.edges()
            if edge.target == identity and edge.status != "containment"
            and edge.kind != "has-variant")

    def callees(self, identity: str) -> tuple[AtlasEdge, ...]:
        identity = self.resolve(identity).identity
        return tuple(
            edge for edge in self.edges()
            if edge.source == identity and edge.status != "containment"
            and edge.kind != "has-variant")

    def unresolved(self) -> tuple[AtlasEdge, ...]:
        return tuple(edge for edge in self.edges()
                     if edge.status in {"unresolved", "frontier"})

    def path(self, source: str, target: str) -> tuple[str, ...]:
        source_id, target_id = self.resolve(source).identity, self.resolve(target).identity
        outgoing: dict[str, list[str]] = {}
        for edge in self.edges():
            if edge.status in {"resolved", "observed"}:
                outgoing.setdefault(edge.source, []).append(edge.target)
        queue = deque([source_id])
        previous: dict[str, str | None] = {source_id: None}
        while queue:
            current = queue.popleft()
            if current == target_id:
                result = []
                while current is not None:
                    result.append(current)
                    current = previous[current]
                return tuple(reversed(result))
            for successor in sorted(outgoing.get(current, ())):
                if successor not in previous:
                    previous[successor] = current
                    queue.append(successor)
        raise KeyError(f"no known path from {source_id} to {target_id}")

    def replay_coverage(self, function_id: str) -> tuple[ReplayCoverage, ...]:
        function_id = self.resolve(function_id).identity
        _, replays = self._indexes()
        return tuple(
            ReplayCoverage(
                str(item["replay_id"]), function_id, int(item["invocation_count"]),
                _point(item.get("first_entry")), _point(item.get("last_exit")),
                _point(item.get("cached_at_or_before_entry")),
                _point(item.get("cached_at_or_before_exit")),
                tuple(map(str, item.get("runtime_variants", ()))),
                bool(item.get("incomplete", False)),
                tuple(dict(annotation)
                      for annotation in item.get("annotations", ())),
            )
            for item in replays["coverage"] if item["function_id"] == function_id
        )

    def best_replay(self, function_id: str) -> ReplayCoverage:
        choices = self.replay_coverage(function_id)
        if not choices:
            raise KeyError(f"no replay covers {function_id!r}")
        return min(choices, key=lambda item: (
            not item.complete,
            item.cached_at_or_before_entry != item.first_entry,
            (2 ** 63 if not item.complete else
             item.last_exit.ordinal - item.first_entry.ordinal),
            -item.invocation_count,
            item.replay_id,
        ))

    def coverage_for(self, product_profile: str) -> ProgramCoverage:
        roots = tuple(self._manifest["product_roots"].get(product_profile, ()))
        if not roots:
            raise KeyError(f"Atlas has no roots for product profile {product_profile!r}")
        node_kinds = {node.identity: node.kind for node in self.nodes()}
        missing = [root for root in roots if root not in node_kinds]
        if missing:
            raise AtlasError("Atlas product roots do not exist: " + ", ".join(missing))
        outgoing: dict[str, list[str]] = {}
        for edge in self.edges():
            if edge.status in {"resolved", "observed"} \
                    and node_kinds.get(edge.target) in {
                        "function", "execution-point", "runtime-code-variant",
                    }:
                outgoing.setdefault(edge.source, []).append(edge.target)
        reachable: set[str] = set()
        queue = deque(roots)
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(sorted(outgoing.get(current, ())))
        contained_points = {
            edge.target for edge in self.edges()
            if edge.status == "containment" and edge.source in reachable
            and node_kinds.get(edge.target) == "execution-point"
        }
        reachable.update(contained_points)
        unresolved = tuple(sorted(
            f"{edge.source} --{edge.kind}--> {edge.target}"
            for edge in self.unresolved() if edge.source in reachable
        ))
        return ProgramCoverage(
            roots, frozenset(reachable), unresolved, self.identity_digest)

    def implementation_view(
        self, catalog: ImplementationCatalog, plan: ExecutionPlan | None = None,
    ) -> tuple[dict[str, Any], ...]:
        selected = {} if plan is None else {
            binding.target: binding.implementation_id for binding in plan.bindings
        }
        result = []
        for node in self.nodes(kind="function"):
            implementations = sorted((
                {
                    "implementation_id": descriptor.implementation_id,
                    "origin": descriptor.origin.value,
                    "category": descriptor.category.value,
                    "properties": sorted(descriptor.properties),
                    "required_capabilities": sorted(descriptor.required_capabilities),
                    "required_services": sorted(descriptor.required_services),
                    "required_assets": sorted(descriptor.required_assets),
                    "verification_evidence": sorted(descriptor.verification_evidence),
                    "implementation_digest": descriptor.implementation_digest,
                    "region_id": descriptor.region_id,
                }
                for descriptor in catalog.implementations
                if node.identity in descriptor.targets
            ), key=lambda item: item["implementation_id"])
            result.append({
                "function_id": node.identity,
                "implementations": implementations,
                "selected": selected.get(node.identity),
            })
        return tuple(result)

    def region_view(
        self, region_id: str, catalog: ImplementationCatalog,
        plan: ExecutionPlan | None = None,
    ) -> dict[str, Any]:
        """Join one catalog region to original nodes and its external edges."""
        descriptors = tuple(
            item for item in catalog.implementations if item.region_id == region_id)
        if not descriptors:
            raise KeyError(region_id)
        covered = frozenset(
            target for item in descriptors for target in item.targets
            if any(node.identity == target for node in self.nodes())
        )
        entering = tuple(
            edge for edge in self.edges()
            if edge.target in covered and edge.source not in covered)
        leaving = tuple(
            edge for edge in self.edges()
            if edge.source in covered and edge.target not in covered)
        selected = frozenset() if plan is None else frozenset(
            binding.implementation_id for binding in plan.bindings
            if binding.target in covered)
        return {
            "region_id": region_id,
            "covered_nodes": tuple(sorted(covered)),
            "entry_edges": entering,
            "exit_edges": leaving,
            "implementations": tuple(sorted(
                item.implementation_id for item in descriptors)),
            "selected_implementations": tuple(sorted(selected)),
            "required_capabilities": tuple(sorted(
                capability for item in descriptors
                for capability in item.required_capabilities)),
            "required_services": tuple(sorted(
                service for item in descriptors for service in item.required_services)),
            "verification_evidence": tuple(sorted(
                evidence for item in descriptors
                for evidence in item.verification_evidence)),
        }
