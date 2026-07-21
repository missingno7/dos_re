"""Progressive replacement, boundary collapse, and feature contracts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from dos_re.execution import (
    BackendAdapter,
    EvidenceGrade,
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
    OverrideCategory,
    ProgramCoverage,
    ProgramEdge,
    RecoveryLevel,
    bind_plan_implementations,
    plan_execution,
    profile_configuration,
)
from dos_re.features import FeatureController, FeaturePolicyError
from dos_re.materialized_plan import (
    load_materialized_plan,
    write_materialized_plan,
)


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
    bind_plan_implementations(
        object(), interpreted, carrier_id=INTERPRETED_CPU_CARRIER
    )
    bind_plan_implementations(
        object(), vmless, carrier_id=GENERATED_VMLESS_CARRIER
    )
    assert interpreted_activations == [(INTERPRETED_CPU_CARRIER, (B,))]
    assert vmless_activations == [(GENERATED_VMLESS_CARRIER, (B,))]


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
