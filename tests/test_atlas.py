from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from dos_re.atlas import AtlasError, ExecutionAtlas, StaleAtlasSourceError
from dos_re.execution import (
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    OverrideCategory,
)
from dos_re.identity import FunctionIdentity, ImageIdentity, ProgramIdentity, real_mode_address
from dos_re.replay import (
    ContinuationState,
    ExecutionProfile,
    ReplayArtifact,
    ReplayEvidenceRecorder,
    ReplayExecutionEvidence,
    ReplayPoint,
)
from dos_re.runtime_code import RuntimeCodeSlot, RuntimeCodeVariant


PROGRAM = ProgramIdentity("fixture:1")
IMAGE = ImageIdentity(PROGRAM, "fixture-exe", "sha256", "a" * 64)
TIMELINE = "fixture-oracle-frames-v1"
ORACLE = ExecutionProfile(
    "fixture-oracle", "oracle", "original-interpreter", str(IMAGE),
    "runtime-v1", "devices-v1", "machine-v1", "canonical-v1")


def function(offset: int) -> str:
    return str(FunctionIdentity(
        IMAGE, "real-mode", real_mode_address(0x1010, offset)))


def write_ir(path):
    document = {
        "ir_version": 0,
        "provenance": {
            "snapshot": "C:/machine/snapshots/oracle",
            "toolchain": "fixture",
        },
        "facts_applied": [],
        "functions": {
            "1010:0100": {
                "entry": "1010:0100", "liftable": True, "refusals": [],
                "exits": ["ret"], "signature": "90c3",
                "calls_near": ["0200"], "calls_far": [], "ints": ["21"],
                "blocks": [{
                    "leader": "0100",
                    "instructions": [
                        {"ip": "0100", "bytes": "ffd0", "kind": "call_ind",
                         "mnemonic": "call ax"},
                        {"ip": "0102", "bytes": "c3", "kind": "ret",
                         "mnemonic": "ret"},
                    ],
                }],
            },
            "1010:0200": {
                "entry": "1010:0200", "liftable": True, "refusals": [],
                "exits": ["ret"], "signature": "c3",
                "calls_near": ["0300"], "calls_far": [], "ints": [],
                "blocks": [{
                    "leader": "0200",
                    "instructions": [
                        {"ip": "0200", "bytes": "c3", "kind": "ret",
                         "mnemonic": "ret"},
                    ],
                }],
            },
            "1010:0400": {
                "entry": "1010:0400", "liftable": False,
                "refusals": [{
                    "ip": "0400", "reason": "unsupported-opcode",
                    "detail": "fixture refusal",
                }],
                "exits": [], "calls_near": [], "calls_far": [], "ints": [],
                "blocks": [],
            },
        },
        "unsupported": [],
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def make_replay(path):
    artifact = ReplayArtifact.create(
        path, timeline_id=TIMELINE, events=(),
        metadata={
            "recording_profile_id": ORACLE.profile_id,
            "end_point": ReplayPoint(8, TIMELINE).to_json(),
        })
    state = ContinuationState("machine-v1", {"cpu": {}}, {"ram": bytes(16)}, 0)
    artifact.register_profile(
        ORACLE, base_point=ReplayPoint(0, TIMELINE), base_state=state)
    artifact.cache(
        ORACLE, ReplayPoint(2, TIMELINE), state,
        metadata={"kind": "function-entry"})
    recorder = ReplayEvidenceRecorder()
    recorder.enter(function(0x100), ReplayPoint(2, TIMELINE))
    recorder.observe_transfer(
        function(0x100), function(0x200), "call", ReplayPoint(3, TIMELINE))
    recorder.enter(function(0x200), ReplayPoint(3, TIMELINE))
    recorder.exit(function(0x200), ReplayPoint(4, TIMELINE))
    recorder.exit(function(0x100), ReplayPoint(6, TIMELINE))
    artifact.set_function_visits(recorder.visits)
    artifact.set_execution_evidence(ORACLE, recorder.evidence(ORACLE))
    return artifact


def test_static_and_replay_sources_build_queries_and_planner_coverage(tmp_path):
    ir = tmp_path / "recovery_ir.json"
    write_ir(ir)
    atlas = ExecutionAtlas.create(
        tmp_path / "atlas", program=PROGRAM,
        product_roots={"development": [function(0x100)]})
    slot = RuntimeCodeSlot(
        (0x1010, 0x500), "dispatch-slot", "main", None, "dispatch",
        (RuntimeCodeVariant(
            (0x1010, 0x500), "installed-a", b"\x90\xc3", "main",
            "staticized", ("fixture",)),),
        None, "observed-installer")
    atlas.import_recovery_ir(
        ir, image=IMAGE, roots=["1010:0100"], runtime_code=[slot])
    replay = make_replay(tmp_path / "replay")
    atlas.ingest_replay(replay.directory)

    assert atlas.resolve("1010:0100").identity == function(0x100)
    assert {edge.target for edge in atlas.callees(function(0x100))} >= {
        function(0x200)}
    assert {edge.source for edge in atlas.callers(function(0x200))} == {
        function(0x100)}
    assert atlas.path(function(0x100), function(0x200)) == (
        function(0x100), function(0x200))
    assert any(edge.kind == "call_ind" for edge in atlas.unresolved())

    coverage = atlas.coverage_for("development")
    assert {function(0x100), function(0x200)} < coverage.reachable
    assert any(":point:" in identity for identity in coverage.reachable)
    assert coverage.unresolved_edges
    assert coverage.evidence_identity == atlas.identity_digest

    best = atlas.best_replay(function(0x100))
    assert best.invocation_count == 1
    assert best.first_entry == ReplayPoint(2, TIMELINE)
    assert best.last_exit == ReplayPoint(6, TIMELINE)
    assert best.cached_at_or_before_entry == best.first_entry

    refused = atlas.resolve("1010:0400")
    assert refused.metadata["liftable"] is False
    assert refused.metadata["refusals"][0]["reason"] == "unsupported-opcode"
    assert len(atlas.nodes(kind="runtime-code-slot")) == 1
    assert len(atlas.nodes(kind="runtime-code-variant")) == 1

    region_id = "fixture:1:region:gameplay"
    catalog = ImplementationCatalog((ImplementationEntry(
        ImplementationDescriptor(
            "generated-main", frozenset({function(0x100), function(0x200)}),
            ImplementationOrigin.GENERATED, OverrideCategory.BASELINE,
            required_capabilities=frozenset({"dos-memory"}),
            verification_evidence=frozenset({"replay:fixture"}),
            region_id=region_id,
        )),))
    view = atlas.implementation_view(catalog)
    assert view[0]["implementations"][0]["origin"] == "generated"
    region = atlas.region_view(region_id, catalog)
    assert region["covered_nodes"] == tuple(sorted({
        function(0x100), function(0x200)}))
    assert region["required_capabilities"] == ("dos-memory",)

    result = subprocess.run(
        [sys.executable, "tools/atlas.py", "validate", str(atlas.directory), "--json"],
        cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["valid"] is True


def test_regeneration_is_byte_deterministic_and_paths_are_portable(tmp_path):
    ir = tmp_path / "ir.json"
    write_ir(ir)
    atlas = ExecutionAtlas.create(tmp_path / "atlas", program=PROGRAM)
    atlas.import_recovery_ir(ir, image=IMAGE)
    before = {
        path.relative_to(atlas.directory): path.read_bytes()
        for path in atlas.directory.rglob("*.json")
    }

    atlas.rematerialize()
    after = {
        path.relative_to(atlas.directory): path.read_bytes()
        for path in atlas.directory.rglob("*.json")
    }

    assert before == after
    source = next((atlas.directory / "sources").glob("static-*.json")).read_text()
    assert "C:/machine" not in source
    assert '"snapshot": "oracle"' in source


def test_stale_source_and_ambiguous_query_fail_loud(tmp_path):
    ir = tmp_path / "ir.json"
    write_ir(ir)
    atlas = ExecutionAtlas.create(tmp_path / "atlas", program=PROGRAM)
    atlas.import_recovery_ir(ir, image=IMAGE)
    source = next((atlas.directory / "sources").glob("static-*.json"))
    source.write_text(source.read_text() + " ", encoding="utf-8")
    # Whitespace does not change canonical content, so make a semantic change.
    raw = json.loads(source.read_text())
    raw["roots"] = ["changed"]
    source.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(StaleAtlasSourceError):
        ExecutionAtlas.open(atlas.directory)

    # A fresh Atlas has two matching labels/identities for a broad address.
    clean = ExecutionAtlas.create(tmp_path / "clean", program=PROGRAM)
    clean.import_recovery_ir(ir, image=IMAGE)
    with pytest.raises(AtlasError, match="ambiguous"):
        clean.resolve("1010")


def test_execution_evidence_rejects_candidate_profile(tmp_path):
    replay = make_replay(tmp_path / "replay")
    candidate = ExecutionProfile(
        "candidate", "candidate", "native", str(IMAGE), "runtime", "devices",
        "machine-v1", "canonical-v1")
    replay.register_profile(
        candidate, base_point=ReplayPoint(0, TIMELINE),
        base_state=ContinuationState(
            "machine-v1", {"cpu": {}}, {"ram": bytes(16)}, 0))

    with pytest.raises(ValueError, match="oracle"):
        replay.set_execution_evidence(
            candidate, ReplayExecutionEvidence(candidate.identity_digest))
