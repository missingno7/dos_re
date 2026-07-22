"""Progressive replacement, boundary collapse, and feature contracts."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from dos_re.execution import (
    BackendAdapter,
    ClosureFindingKind,
    EvidenceGrade,
    ExecutionRegionContract,
    ExecutionPolicy,
    FeatureCatalog,
    FeatureCategory,
    FeatureDescriptor,
    GENERATED_VMLESS_CARRIER,
    INTERPRETED_CPU_CARRIER,
    ImplementationCatalog,
    ImplementationContract,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    NATIVE_STATE_CARRIER,
    OverrideCategory,
    ProgramCoverage,
    ProgramEdge,
    RecoveryLevel,
    RegionAdapter,
    RegionEntryPoint,
    RegionExitVerificationContract,
    RegionExitPoint,
    RegionStateOwnership,
    RegionVerificationContract,
    VerificationProjectionContract,
    VerificationRepresentation,
    bind_plan_implementations,
    plan_execution,
    profile_configuration,
)
from dos_re.features import FeatureController, FeaturePolicyError
from dos_re.materialized_plan import (
    load_materialized_plan,
    write_materialized_plan,
)
from dos_re.regions import RegionDispatcher, RegionProgress


ROOT = "program:test"
A = "function:a"
B = "function:b"
CONTRACT = ImplementationContract(
    "test:word-transform/v1",
    inputs=("word",),
    outputs=("word",),
    observable_effects=("return-value",),
)
COVERAGE = ProgramCoverage(
    roots=(ROOT,),
    reachable=frozenset({ROOT, A, B}),
    evidence_identity="test-coverage-v1",
    edges=(
        ProgramEdge(ROOT, A, "call", "static"),
        ProgramEdge(A, B, "call", "replay:smoke"),
    ),
)


def _semantic(value: int) -> int:
    return (value + 1) & 0xFFFF


def _region_verification(exit_target: str) -> RegionVerificationContract:
    interior = VerificationProjectionContract(
        "test:region:interior", VerificationRepresentation.SEMANTIC_STATE,
        "test:semantic/v1", required_fields=("state",),
    )
    seam = VerificationProjectionContract(
        "test:region:exit", VerificationRepresentation.CONTINUATION_SEAM,
        "test:semantic/v1", required_fields=("continuation",),
        required_regions=("shared-memory",),
    )
    return RegionVerificationContract(
        "test:region/v1", interior,
        (RegionExitVerificationContract("complete", exit_target, seam),),
    )


def _provider(identifier: str, carrier: str, level: RecoveryLevel):
    return ImplementationEntry(ImplementationDescriptor(
        implementation_id=identifier,
        targets=COVERAGE.reachable,
        origin=(
            ImplementationOrigin.INTERPRETED
            if level is RecoveryLevel.INTERPRETED
            else ImplementationOrigin.GENERATED
        ),
        recovery_level=level,
        execution_carrier=carrier,
        region_id=ROOT,
        implementation_digest=identifier + "-v1",
    ))


def _authored(activated: list[tuple[str, tuple[str, ...]]]):
    def activate(carrier: str):
        return lambda runtime, targets: activated.append((carrier, targets))

    return ImplementationEntry(
        ImplementationDescriptor(
            implementation_id="authored:b",
            targets=frozenset({B}),
            origin=ImplementationOrigin.AUTHORED,
            category=OverrideCategory.FAITHFUL,
            recovery_level=RecoveryLevel.AUTHORED_NATIVE,
            evidence_grade=EvidenceGrade.REPLAY_CORPUS,
            verification_evidence=frozenset({"replay:smoke"}),
            contract=CONTRACT,
            implementation_digest="authored-b-v1",
        ),
        implementation=_semantic,
        adapters=(
            BackendAdapter(
                "authored:b/interpreted",
                INTERPRETED_CPU_CARRIER,
                activate(INTERPRETED_CPU_CARRIER),
                "adapter-interpreted-v1",
            ),
            BackendAdapter(
                "authored:b/vmless",
                GENERATED_VMLESS_CARRIER,
                activate(GENERATED_VMLESS_CARRIER),
                "adapter-vmless-v1",
            ),
        ),
    )


def _catalog(activated):
    return ImplementationCatalog((
        _provider(
            "baseline:interpreted", INTERPRETED_CPU_CARRIER,
            RecoveryLevel.INTERPRETED,
        ),
        _provider(
            "baseline:vmless", GENERATED_VMLESS_CARRIER,
            RecoveryLevel.GENERATED_VMLESS,
        ),
        _authored(activated),
    ))


def _plan(provider: str, *, authored: bool = True):
    activated: list[tuple[str, tuple[str, ...]]] = []
    config = profile_configuration(
        "development",
        program_identity=ROOT,
        provider_preference=(provider,),
        selected_overrides=("authored:b",) if authored else (),
    )
    if authored:
        config = replace(
            config,
            execution_policy=replace(
                config.execution_policy,
                minimum_authored_evidence=EvidenceGrade.REPLAY_CORPUS,
            ),
        )
    return plan_execution(config, COVERAGE, _catalog(activated)), activated


def test_same_authored_body_binds_through_two_carriers() -> None:
    interpreted, interpreted_activations = _plan("baseline:interpreted")
    vmless, vmless_activations = _plan("baseline:vmless")

    assert interpreted.catalog.implementation("authored:b") is (
        vmless.catalog.implementation("authored:b")
    )
    interpreted_runtime = SimpleNamespace()
    vmless_runtime = SimpleNamespace()
    bind_plan_implementations(
        interpreted_runtime, interpreted, carrier_id=INTERPRETED_CPU_CARRIER
    )
    bind_plan_implementations(
        vmless_runtime, vmless, carrier_id=GENERATED_VMLESS_CARRIER
    )
    assert interpreted_activations == [(INTERPRETED_CPU_CARRIER, (B,))]
    assert vmless_activations == [(GENERATED_VMLESS_CARRIER, (B,))]
    assert interpreted_runtime.execution_plan is interpreted
    assert interpreted_runtime.execution_carrier_id == INTERPRETED_CPU_CARRIER
    assert vmless_runtime.execution_plan is vmless
    assert vmless_runtime.execution_carrier_id == GENERATED_VMLESS_CARRIER


def test_larger_provider_collapses_known_hook_boundaries() -> None:
    mixed, _ = _plan("baseline:vmless")
    fallback, _ = _plan("baseline:vmless", authored=False)

    assert mixed.report.execution_carrier == GENERATED_VMLESS_CARRIER
    assert [(item.source, item.target, item.adapter_id)
            for item in mixed.report.active_boundaries] == [
        (A, B, "authored:b/vmless"),
    ]
    assert mixed.report.collapsed_edge_count == 1
    assert fallback.report.active_boundaries == ()
    assert fallback.report.collapsed_edge_count == 2
    assert dict((item.target, item.implementation_id)
                for item in fallback.bindings)[B] == "baseline:vmless"


def test_insufficient_finite_evidence_falls_back_with_reason() -> None:
    activated: list[tuple[str, tuple[str, ...]]] = []
    authored = _authored(activated)
    authored = replace(
        authored,
        descriptor=replace(
            authored.descriptor, evidence_grade=EvidenceGrade.FOCUSED
        ),
    )
    config = profile_configuration(
        "development",
        program_identity=ROOT,
        provider_preference=("baseline:vmless",),
        selected_overrides=("authored:b",),
    )
    config = replace(
        config,
        execution_policy=replace(
            config.execution_policy,
            minimum_authored_evidence=EvidenceGrade.REPLAY_CORPUS,
        ),
    )
    plan = plan_execution(config, COVERAGE, ImplementationCatalog((
        _provider(
            "baseline:vmless", GENERATED_VMLESS_CARRIER,
            RecoveryLevel.GENERATED_VMLESS,
        ),
        authored,
    )))
    decision = next(
        item for item in plan.report.target_resolutions if item.target == B
    )
    rejected = next(
        item for item in decision.candidates
        if item.implementation_id == "authored:b"
    )
    assert rejected.selected is False
    assert rejected.rejection_reasons == (
        "insufficient evidence: focused < replay_corpus",
    )


def test_behavioral_feature_is_recorded_and_applied_only_at_safe_boundary() -> None:
    feature = FeatureDescriptor(
        "test:invulnerability",
        FeatureCategory.BEHAVIORAL,
        changes_authoritative_state=True,
        replay_channel="test:features/v1",
        safe_boundaries=frozenset({"game-tick"}),
        feature_digest="invulnerability-v1",
    )
    config = profile_configuration(
        "development",
        program_identity=ROOT,
        provider_preference=("baseline:vmless",),
        enabled_features=(feature.feature_id,),
    )
    plan = plan_execution(
        config,
        COVERAGE,
        ImplementationCatalog((_provider(
            "baseline:vmless", GENERATED_VMLESS_CARRIER,
            RecoveryLevel.GENERATED_VMLESS,
        ),)),
        feature_catalog=FeatureCatalog((feature,)),
    )
    controller = FeatureController(plan.features)
    recorded = []
    payload = controller.request(
        feature.feature_id,
        True,
        ordinal=7,
        record_event=lambda *event: recorded.append(event),
    )
    applied = []
    assert controller.apply_pending("presentation", lambda *x: applied.append(x)) == ()
    assert controller.apply_pending("game-tick", lambda *x: applied.append(x))
    assert recorded == [(7, "test:features/v1", payload)]
    assert applied == [(feature.feature_id, True)]

    with pytest.raises(FeaturePolicyError, match="must be recorded"):
        controller.request(feature.feature_id, False, ordinal=8)


def test_materialized_plan_contains_final_binding_graph_without_planner(tmp_path) -> None:
    plan, _ = _plan("baseline:vmless")
    path = write_materialized_plan(plan, tmp_path / "execution_plan.json")
    payload = load_materialized_plan(path)

    assert payload["execution_carrier"] == GENERATED_VMLESS_CARRIER
    assert payload["bindings"][B] == "authored:b"
    assert payload["implementations"]["authored:b"]["adapter"] == {
        "digest": "adapter-vmless-v1",
        "id": "authored:b/vmless",
    }


def test_long_lived_region_collapses_contextual_targets_and_materializes_handoffs(
    tmp_path,
) -> None:
    region_id = "region:gameplay"
    entry_target = "point:start-gameplay"
    exit_target = "point:return-menu"
    region_coverage = ProgramCoverage(
        roots=(ROOT,),
        reachable=frozenset({ROOT, A, B, region_id, entry_target, exit_target}),
        unresolved_edges=(f"{A} --call--> {B}",),
        evidence_identity="test-region-coverage-v1",
        edges=(
            ProgramEdge(A, entry_target, "region-entry", "manual"),
            ProgramEdge(entry_target, region_id, "handoff", "manual"),
            ProgramEdge(region_id, exit_target, "region-exit", "manual"),
            ProgramEdge(exit_target, A, "continuation", "manual"),
        ),
    )
    activated = []
    inner_activations: list[tuple[str, tuple[str, ...]]] = []
    inner = _authored(inner_activations)
    contract = ExecutionRegionContract(
        region_id=region_id,
        carrier_id=NATIVE_STATE_CARRIER,
        state_ownership=RegionStateOwnership.SHARED_DOS_MEMORY,
        entries=(RegionEntryPoint("start", entry_target),),
        exits=(RegionExitPoint("complete", exit_target),),
        covered_targets=frozenset({A, B}),
        replay_boundaries=frozenset({"game-tick"}),
        state_inputs=("DOS memory",),
        state_outputs=("DOS memory", "presentation"),
        verification=_region_verification(exit_target),
    )
    island = ImplementationEntry(
        ImplementationDescriptor(
            implementation_id="authored:gameplay-region",
            targets=frozenset({region_id, entry_target, exit_target}),
            origin=ImplementationOrigin.AUTHORED,
            category=OverrideCategory.FAITHFUL,
            recovery_level=RecoveryLevel.AUTHORED_NATIVE,
            evidence_grade=EvidenceGrade.REPLAY_CORPUS,
            execution_carrier=NATIVE_STATE_CARRIER,
            region_id=region_id,
            region_contract=contract,
            implementation_digest="authored-gameplay-region-v1",
        ),
        region_adapters=(RegionAdapter(
            "authored:gameplay-region/vmless",
            GENERATED_VMLESS_CARRIER,
            NATIVE_STATE_CARRIER,
            lambda runtime, binding: activated.append((runtime, binding)),
            "region-adapter-v1",
        ),),
    )
    config = profile_configuration(
        "development",
        program_identity=ROOT,
        provider_preference=("baseline:vmless",),
        selected_overrides=(
            inner.descriptor.implementation_id,
            island.descriptor.implementation_id,
        ),
    )
    config = replace(
        config,
        execution_policy=replace(
            config.execution_policy,
            minimum_authored_evidence=EvidenceGrade.REPLAY_CORPUS,
        ),
    )
    plan = plan_execution(
        config,
        region_coverage,
        ImplementationCatalog((
            ImplementationEntry(replace(
                _provider(
                    "baseline:vmless", GENERATED_VMLESS_CARRIER,
                    RecoveryLevel.GENERATED_VMLESS,
                ).descriptor,
                targets=region_coverage.reachable,
            )),
            inner,
            island,
        )),
    )

    assert len(plan.regions) == 1
    resolved = plan.regions[0]
    assert resolved.verification is not None
    assert resolved.verification.contract_id == "test:region/v1"
    assert resolved.covered_targets == (A, B)
    assert {item.target for item in resolved.suppressed_bindings} == {A, B}
    assert plan.report.unresolved_edges == ()
    assert plan.report.closure_findings[0].classification is (
        ClosureFindingKind.REGION_COLLAPSED
    )
    runtime = SimpleNamespace()
    bind_plan_implementations(
        runtime, plan, carrier_id=GENERATED_VMLESS_CARRIER
    )
    assert activated == [(runtime, resolved)]
    # The region contextually owns A/B while active, but B's selected adapter
    # remains available to surrounding carrier code outside the region.
    assert inner_activations == [(GENERATED_VMLESS_CARRIER, (B,))]

    class Session:
        def __init__(self):
            self.steps = 0

        def advance(self):
            self.steps += 1
            return (
                RegionProgress.yielded("game-tick")
                if self.steps == 1
                else RegionProgress.exited("complete")
            )

    completed = []
    dispatcher = RegionDispatcher()
    dispatcher.enter(
        resolved, "start", Session(), complete=completed.append,
    )
    assert dispatcher.advance() == RegionProgress.yielded("game-tick")
    assert dispatcher.active
    assert dispatcher.advance() == RegionProgress.exited("complete")
    assert completed == [RegionExitPoint("complete", exit_target)]
    assert not dispatcher.active

    dispatcher.enter(
        resolved, "start", Session(), complete=completed.append,
    )
    assert dispatcher.advance() == RegionProgress.yielded("game-tick")
    dispatcher.reset()
    assert not dispatcher.active
    assert dispatcher.active_region_id == ""
    assert dispatcher.last_region_id == ""
    assert dispatcher.last_entry_id == ""
    assert dispatcher.last_exit_id == ""

    payload = load_materialized_plan(
        write_materialized_plan(plan, tmp_path / "execution_plan.json")
    )
    assert payload["regions"][region_id]["adapter"] == {
        "id": "authored:gameplay-region/vmless",
        "digest": "region-adapter-v1",
    }
