"""Contracts for the dos_re 3.0 execution planner."""
from __future__ import annotations

import pytest

from dos_re.execution import (
    BootstrapArtifact,
    BuildImageBootstrapProvider,
    BuildTarget,
    CompositeBootstrapProvider,
    DependencyCapability,
    ExecutionPlanError,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    ExeBootstrapProvider,
    NativeBootstrapProvider,
    OverrideCategory,
    ProgramCoverage,
    RuntimeServiceCatalog,
    RuntimeServiceDescriptor,
    RuntimeCapabilityViolation,
    execution_composition_digest,
    plan_execution,
    profile_configuration,
)


PROGRAM = "game:sha256"
ROOT = "function:root"
CALLEE = "function:callee"
COVERAGE = ProgramCoverage(
    roots=(ROOT,),
    reachable=frozenset({ROOT, CALLEE}),
    evidence_identity="coverage-v1",
)


def _catalog(*items):
    return ImplementationCatalog(tuple(ImplementationEntry(item) for item in items))


def _services(*items):
    return RuntimeServiceCatalog(tuple(items))


def _implementation(
    implementation_id: str,
    targets,
    *,
    origin=ImplementationOrigin.GENERATED,
    category=OverrideCategory.BASELINE,
    exe=False,
    interpreter=False,
    capabilities=(),
    services=(),
    digest="v1",
):
    return ImplementationDescriptor(
        implementation_id=implementation_id,
        targets=frozenset(targets),
        origin=origin,
        category=category,
        required_capabilities=frozenset(capabilities) | (
            {DependencyCapability.ORIGINAL_EXE.value} if exe else set()
        ) | (
            {DependencyCapability.INTERPRETER.value} if interpreter else set()
        ),
        required_services=frozenset(services),
        implementation_digest=digest,
    )


def test_development_plan_may_mix_interpreted_and_generated():
    config = profile_configuration(
        "development",
        program_identity=PROGRAM,
        provider_preference=("generated-root", "interpreted"),
    )
    plan = plan_execution(config, COVERAGE, _catalog(
        _implementation("generated-root", (ROOT,)),
        _implementation("interpreted", (ROOT, CALLEE), exe=True, interpreter=True),
    ))
    assert dict((item.target, item.implementation_id) for item in plan.bindings) == {
        ROOT: "generated-root",
        CALLEE: "interpreted",
    }
    assert plan.report.requires(DependencyCapability.ORIGINAL_EXE)
    assert not plan.report.is_detached_from(DependencyCapability.ORIGINAL_EXE)


def test_detached_rejects_exe_only_frontier_with_actionable_report():
    config = profile_configuration("detached", program_identity=PROGRAM)
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, _catalog(
            _implementation(
                "interpreted", (ROOT, CALLEE), exe=True, interpreter=True
            ),
        ))
    report = caught.value.report
    assert report.unresolved == (CALLEE, ROOT)
    assert report.policy_forbidden_capabilities == (
        DependencyCapability.INTERPRETER.value,
        DependencyCapability.ORIGINAL_EXE.value,
    )
    assert "required capabilities forbidden" in str(caught.value)


def test_detached_accepts_mixed_non_exe_recovery_properties():
    config = profile_configuration(
        "detached",
        program_identity=PROGRAM,
        provider_preference=("vm-root", "cpu-free-callee"),
    )
    plan = plan_execution(config, COVERAGE, _catalog(
        _implementation("vm-root", (ROOT,)),
        _implementation("cpu-free-callee", (CALLEE,)),
    ))
    assert plan.report.is_detached_from(DependencyCapability.ORIGINAL_EXE)
    assert plan.report.is_detached_from(DependencyCapability.INTERPRETER)
    assert not plan.report.package_ready  # detachment is not packaging


def test_release_rejects_development_only_service():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation("external", (ROOT, CALLEE), services=("trace",))
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, _catalog(implementation), _services(
            RuntimeServiceDescriptor("trace", product_safe=False),
        ))
    assert caught.value.report.development_only_services == ("trace",)


def test_detached_allows_diagnostics_but_rejects_profiler_capability():
    config = profile_configuration("detached", program_identity=PROGRAM)
    implementation = _implementation(
        "external", (ROOT, CALLEE), services=("diagnostic", "profiler")
    )
    services = (
        RuntimeServiceDescriptor(
            "diagnostic",
            product_safe=False,
            required_capabilities=frozenset({"diagnostics"}),
        ),
        RuntimeServiceDescriptor(
            "profiler",
            product_safe=False,
            required_capabilities=frozenset({"profiling"}),
        ),
    )
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, _catalog(implementation), _services(*services))
    assert caught.value.report.policy_forbidden_services == ("profiler",)


def test_release_plan_is_package_ready_with_product_safe_closure():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation("external", (ROOT, CALLEE), services=("display",))
    plan = plan_execution(config, COVERAGE, _catalog(implementation), _services(
        RuntimeServiceDescriptor(
            "display", product_safe=True, implementation_digest="display-v1"
        ),
    ))
    assert plan.report.is_detached_from(DependencyCapability.ORIGINAL_EXE)
    assert plan.report.package_ready
    assert len(plan.plan_digest) == 64


def test_dependency_closure_combines_implementation_product_and_service_requirements():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        product_services=("display",),
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation(
        "external",
        (ROOT, CALLEE),
        capabilities=(DependencyCapability.DOS_MEMORY.value,),
        services=("storage",),
    )
    plan = plan_execution(config, COVERAGE, _catalog(implementation), _services(
        RuntimeServiceDescriptor(
            "display",
            product_safe=True,
            required_capabilities=frozenset({"host-display"}),
            dependencies=frozenset({"storage"}),
        ),
        RuntimeServiceDescriptor(
            "storage",
            product_safe=True,
            required_capabilities=frozenset({"host-filesystem"}),
        ),
    ))
    assert plan.report.required_services == ("display", "storage")
    assert plan.report.required_capabilities == (
        DependencyCapability.DOS_MEMORY.value,
        "host-display",
        "host-filesystem",
    )
    uses = {item.capability: item.consumers for item in plan.report.capability_uses}
    assert uses[DependencyCapability.DOS_MEMORY.value] == (
        "implementation:external",
    )
    assert uses["host-display"] == ("service:display",)
    assert plan.report.is_detached_from(DependencyCapability.CPU_MODEL)
    assert not plan.report.is_detached_from(DependencyCapability.DOS_MEMORY)


def test_release_rejects_oracle_capability_even_when_service_is_product_safe():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation("external", (ROOT, CALLEE), services=("oracle",))
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, _catalog(implementation), _services(
            RuntimeServiceDescriptor(
                "oracle",
                product_safe=True,
                required_capabilities=frozenset({
                    DependencyCapability.ORACLE.value,
                }),
            ),
        ))
    assert caught.value.report.policy_forbidden_capabilities == (
        DependencyCapability.ORACLE.value,
    )
    assert caught.value.report.policy_forbidden_services == ("oracle",)


def test_release_rejects_exe_runtime_bootstrap_even_with_native_coverage(tmp_path):
    exe = tmp_path / "GAME.EXE"
    exe.write_bytes(b"MZ")
    bootstrap = ExeBootstrapProvider(
        "exe-loader",
        ("machine state",),
        artifacts=(BootstrapArtifact(
            "game-exe",
            "GAME.EXE",
            str(exe),
        ),),
    )
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(
            profile_configuration(
                "release",
                program_identity=PROGRAM,
                bootstrap_provider=bootstrap,
                build_target=BuildTarget("windows", "zip"),
            ),
            COVERAGE,
            _catalog(_implementation("native", (ROOT, CALLEE))),
        )
    report = caught.value.report
    assert report.bootstrap_provider_id == "exe-loader"
    assert report.policy_forbidden_capabilities == (
        DependencyCapability.ORIGINAL_CODE.value,
        DependencyCapability.ORIGINAL_EXE.value,
    )


def test_build_image_bootstrap_retains_build_exe_but_not_runtime_exe(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    bootstrap = BuildImageBootstrapProvider(
        "build-image",
        ("CPU registers", "DOS memory"),
        artifacts=(BootstrapArtifact(
            "boot-state",
            "bootstrap/state.json",
            str(state),
            generation_instruction="python scripts/build_boot_image.py",
        ),),
        build_required_capabilities=frozenset({
            DependencyCapability.ORIGINAL_EXE.value,
        }),
        runtime_required_capabilities=frozenset({
            DependencyCapability.DOS_MEMORY.value,
        }),
        initialized_capabilities=frozenset({
            DependencyCapability.CPU_MODEL.value,
            DependencyCapability.DOS_MEMORY.value,
        }),
        valid_profiles=frozenset({"detached", "release"}),
    )
    plan = plan_execution(
        profile_configuration(
            "release",
            program_identity=PROGRAM,
            bootstrap_provider=bootstrap,
            build_target=BuildTarget("windows", "zip"),
        ),
        COVERAGE,
        _catalog(_implementation("native", (ROOT, CALLEE))),
    )
    assert plan.report.bootstrap_build_capabilities == (
        DependencyCapability.ORIGINAL_EXE.value,
    )
    assert plan.report.is_detached_from(DependencyCapability.ORIGINAL_EXE)
    assert not plan.report.is_detached_from(DependencyCapability.DOS_MEMORY)
    assert plan.report.package_ready


def test_missing_bootstrap_artifact_fails_with_generation_instruction(tmp_path):
    bootstrap = BuildImageBootstrapProvider(
        "missing-image",
        ("machine state",),
        artifacts=(BootstrapArtifact(
            "boot-state",
            "bootstrap/state.json",
            str(tmp_path / "missing.json"),
            generation_instruction="run: python scripts/build_boot_image.py",
        ),),
        valid_profiles=frozenset({"release"}),
    )
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(
            profile_configuration(
                "release",
                program_identity=PROGRAM,
                bootstrap_provider=bootstrap,
                build_target=BuildTarget("windows", "zip"),
            ),
            COVERAGE,
            _catalog(_implementation("native", (ROOT, CALLEE))),
        )
    assert "python scripts/build_boot_image.py" in str(caught.value)


def test_composite_bootstrap_unifies_component_state_and_capabilities():
    bootstrap = CompositeBootstrapProvider(
        "composite",
        ("product ready",),
        providers=(
            NativeBootstrapProvider(
                "native-state",
                ("gameplay state",),
                initialized_capabilities=frozenset({"native-state"}),
            ),
            NativeBootstrapProvider(
                "device-state",
                ("device state",),
                runtime_required_capabilities=frozenset({"host-audio"}),
            ),
        ),
    )
    plan = plan_execution(
        profile_configuration(
            "development",
            program_identity=PROGRAM,
            bootstrap_provider=bootstrap,
        ),
        COVERAGE,
        _catalog(_implementation("native", (ROOT, CALLEE))),
    )
    assert plan.report.bootstrap_kind == "composite"
    assert plan.report.bootstrap_state_outputs == (
        "device state",
        "gameplay state",
        "product ready",
    )
    assert plan.report.requires("host-audio")


def test_runtime_capability_guard_rejects_undeclared_fallback():
    plan = plan_execution(
        profile_configuration("detached", program_identity=PROGRAM),
        COVERAGE,
        _catalog(_implementation("external", (ROOT, CALLEE))),
    )
    with pytest.raises(RuntimeCapabilityViolation, match="fallback:function:callee"):
        plan.require_capability(
            DependencyCapability.INTERPRETER,
            consumer="fallback:function:callee",
        )


def test_release_rejects_implementation_incompatible_with_build_target():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("mobile", "bundle"),
    )
    implementation = ImplementationDescriptor(
        implementation_id="windows-only",
        targets=frozenset({ROOT, CALLEE}),
        origin=ImplementationOrigin.GENERATED,
        supported_platforms=frozenset({"windows"}),
    )
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, _catalog(implementation))
    assert caught.value.report.packaging_incompatible == (
        f"{CALLEE}:windows-only",
        f"{ROOT}:windows-only",
    )


def test_report_names_alternative_that_removes_dependency():
    plan = plan_execution(
        profile_configuration(
            "development",
            program_identity=PROGRAM,
            provider_preference=("interpreted",),
        ),
        COVERAGE,
        _catalog(
            _implementation(
                "interpreted",
                (ROOT, CALLEE),
                capabilities=(DependencyCapability.CPU_MODEL.value,),
            ),
            _implementation("cpu-free", (CALLEE,)),
        ),
    )
    blocker = next(
        item for item in plan.report.capability_blockers
        if item.target == CALLEE
        and item.capability == DependencyCapability.CPU_MODEL.value
    )
    assert blocker.implementation_id == "interpreted"
    assert blocker.alternatives_without_capability == ("cpu-free",)


def test_plan_digest_changes_with_implementation_evidence():
    config = profile_configuration("detached", program_identity=PROGRAM)
    first = plan_execution(config, COVERAGE, _catalog(
        _implementation("external", (ROOT, CALLEE), digest="one"),
    ))
    second = plan_execution(config, COVERAGE, _catalog(
        _implementation("external", (ROOT, CALLEE), digest="two"),
    ))
    assert first.plan_digest != second.plan_digest


def test_execution_composition_digest_ignores_coverage_evidence():
    config = profile_configuration("detached", program_identity=PROGRAM)
    implementation = _implementation("external", (ROOT, CALLEE), digest="one")
    first = plan_execution(config, COVERAGE, _catalog(implementation))
    second = plan_execution(
        config,
        ProgramCoverage(
            roots=COVERAGE.roots,
            reachable=COVERAGE.reachable,
            evidence_identity="coverage-v2-with-replay-evidence",
        ),
        _catalog(implementation),
    )

    assert first.plan_digest != second.plan_digest
    assert (
        execution_composition_digest(first)
        == execution_composition_digest(second)
    )


def test_authored_implementation_requires_explicit_selection():
    authored = _implementation(
        "handwritten",
        (ROOT,),
        origin=ImplementationOrigin.AUTHORED,
        category=OverrideCategory.FAITHFUL,
    )
    baseline = _implementation("generated", (ROOT, CALLEE))
    without = plan_execution(
        profile_configuration("detached", program_identity=PROGRAM),
        COVERAGE,
        _catalog(authored, baseline),
    )
    assert dict((item.target, item.implementation_id) for item in without.bindings)[ROOT] == (
        "generated"
    )

    with_selected = plan_execution(
        profile_configuration(
            "detached",
            program_identity=PROGRAM,
            selected_overrides=("handwritten",),
            provider_preference=("handwritten", "generated"),
        ),
        COVERAGE,
        _catalog(authored, baseline),
    )
    assert dict(
        (item.target, item.implementation_id) for item in with_selected.bindings
    )[ROOT] == "handwritten"
    assert with_selected.report.faithful_override_coverage == (ROOT,)
