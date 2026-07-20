"""Contracts for the dos_re 3.0 execution planner."""
from __future__ import annotations

import pytest

from dos_re.execution import (
    BuildTarget,
    CoverageResult,
    ExecutionPlanError,
    ImplementationDescriptor,
    ImplementationOrigin,
    OverrideCategory,
    RuntimeServiceDescriptor,
    StaticCoverageSource,
    plan_execution,
    profile_configuration,
)


PROGRAM = "game:sha256"
ROOT = "function:root"
CALLEE = "function:callee"
COVERAGE = StaticCoverageSource(CoverageResult(
    roots=(ROOT,),
    reachable=frozenset({ROOT, CALLEE}),
    evidence_identity="coverage-v1",
))


def _implementation(
    implementation_id: str,
    targets,
    *,
    origin=ImplementationOrigin.GENERATED,
    category=OverrideCategory.BASELINE,
    exe=False,
    interpreter=False,
    services=(),
    digest="v1",
):
    return ImplementationDescriptor(
        implementation_id=implementation_id,
        targets=frozenset(targets),
        origin=origin,
        category=category,
        requires_original_exe=exe,
        requires_interpreter=interpreter,
        required_services=frozenset(services),
        implementation_digest=digest,
    )


def test_development_plan_may_mix_interpreted_and_generated():
    config = profile_configuration(
        "development",
        program_identity=PROGRAM,
        provider_preference=("generated-root", "interpreted"),
    )
    plan = plan_execution(config, COVERAGE, (
        _implementation("generated-root", (ROOT,)),
        _implementation("interpreted", (ROOT, CALLEE), exe=True, interpreter=True),
    ))
    assert dict((item.target, item.implementation_id) for item in plan.bindings) == {
        ROOT: "generated-root",
        CALLEE: "interpreted",
    }
    assert plan.report.exe_dependent == (CALLEE,)
    assert not plan.report.standalone_executable_ready


def test_detached_rejects_exe_only_frontier_with_actionable_report():
    config = profile_configuration("detached", program_identity=PROGRAM)
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, (
            _implementation(
                "interpreted", (ROOT, CALLEE), exe=True, interpreter=True
            ),
        ))
    report = caught.value.report
    assert report.unresolved == (CALLEE, ROOT)
    assert report.exe_dependent == (CALLEE, ROOT)
    assert report.interpreter_dependent == (CALLEE, ROOT)
    assert "original-EXE-dependent" in str(caught.value)


def test_detached_accepts_mixed_non_exe_recovery_properties():
    config = profile_configuration(
        "detached",
        program_identity=PROGRAM,
        provider_preference=("vm-root", "cpu-free-callee"),
    )
    plan = plan_execution(config, COVERAGE, (
        _implementation("vm-root", (ROOT,)),
        _implementation("cpu-free-callee", (CALLEE,)),
    ))
    assert plan.report.standalone_executable_ready
    assert not plan.report.package_ready  # detachment is not packaging


def test_release_rejects_development_only_service():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation("external", (ROOT, CALLEE), services=("trace",))
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, (implementation,), (
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
            development_capabilities=frozenset({"diagnostics"}),
        ),
        RuntimeServiceDescriptor(
            "profiler",
            product_safe=False,
            development_capabilities=frozenset({"profiling"}),
        ),
    )
    with pytest.raises(ExecutionPlanError) as caught:
        plan_execution(config, COVERAGE, (implementation,), services)
    assert caught.value.report.policy_forbidden_services == ("profiler",)


def test_release_plan_is_package_ready_with_product_safe_closure():
    config = profile_configuration(
        "release",
        program_identity=PROGRAM,
        build_target=BuildTarget("windows", "zip"),
    )
    implementation = _implementation("external", (ROOT, CALLEE), services=("display",))
    plan = plan_execution(config, COVERAGE, (implementation,), (
        RuntimeServiceDescriptor(
            "display", product_safe=True, implementation_digest="display-v1"
        ),
    ))
    assert plan.report.standalone_executable_ready
    assert plan.report.package_ready
    assert len(plan.plan_digest) == 64


def test_plan_digest_changes_with_implementation_evidence():
    config = profile_configuration("detached", program_identity=PROGRAM)
    first = plan_execution(config, COVERAGE, (
        _implementation("external", (ROOT, CALLEE), digest="one"),
    ))
    second = plan_execution(config, COVERAGE, (
        _implementation("external", (ROOT, CALLEE), digest="two"),
    ))
    assert first.plan_digest != second.plan_digest


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
        (authored, baseline),
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
        (authored, baseline),
    )
    assert dict(
        (item.target, item.implementation_id) for item in with_selected.bindings
    )[ROOT] == "handwritten"
    assert with_selected.report.faithful_override_coverage == (ROOT,)
