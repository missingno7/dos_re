"""Execution composition and dependency-policy planning for dos_re 3.0.

This module is deliberately backend-neutral.  It does not import a CPU,
interpreter, player, replay implementation, or the future Execution Atlas.
Ports describe coverage, implementations, and services as immutable records;
the planner selects one implementation per reachable identity and fails before
runtime construction when a strict profile cannot be satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Callable, Iterable, Protocol


class Requirement(str, Enum):
    """Whether an execution dependency is required, permitted, or forbidden."""

    REQUIRED = "required"
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"


class DynamicLoading(str, Enum):
    ALLOWED = "allowed"
    DECLARED_ONLY = "declared-only"
    FORBIDDEN = "forbidden"


class ImplementationOrigin(str, Enum):
    INTERPRETED = "interpreted"
    GENERATED = "generated"
    AUTHORED = "authored"


class OverrideCategory(str, Enum):
    BASELINE = "baseline"
    FAITHFUL = "faithful"
    ENHANCEMENT = "enhancement"
    BEHAVIORAL = "behavioral"


@dataclass(frozen=True)
class ExecutionPolicy:
    original_exe: Requirement
    interpreter: Requirement
    development_capabilities: frozenset[str] = frozenset()
    dynamic_loading: DynamicLoading = DynamicLoading.ALLOWED
    strict_coverage: bool = False


@dataclass(frozen=True)
class VerificationPolicy:
    mode: str = "none"
    oracle_required: bool = False


@dataclass(frozen=True)
class BuildTarget:
    platform: str
    package_format: str = ""


@dataclass(frozen=True)
class ExecutionConfiguration:
    program_identity: str
    profile: str
    product_profile: str
    execution_policy: ExecutionPolicy
    verification_policy: VerificationPolicy = VerificationPolicy()
    provider_preference: tuple[str, ...] = ()
    selected_overrides: tuple[str, ...] = ()
    build_target: BuildTarget | None = None


@dataclass(frozen=True)
class ProgramCoverage:
    """Conservative reachable program identities for one product profile."""

    roots: tuple[str, ...]
    reachable: frozenset[str]
    unresolved_edges: tuple[str, ...] = ()
    evidence_identity: str = ""

    def coverage_for(self, product_profile: str) -> "ProgramCoverage":
        return self


class CoverageSource(Protocol):
    """Interface current IR adapters and the future Execution Atlas implement."""

    def coverage_for(self, product_profile: str) -> ProgramCoverage: ...


@dataclass(frozen=True)
class ImplementationDescriptor:
    implementation_id: str
    targets: frozenset[str]
    origin: ImplementationOrigin
    category: OverrideCategory = OverrideCategory.BASELINE
    properties: frozenset[str] = frozenset()
    requires_original_exe: bool = False
    requires_interpreter: bool = False
    required_services: frozenset[str] = frozenset()
    implementation_digest: str = ""
    region_id: str | None = None


@dataclass(frozen=True)
class RuntimeServiceDescriptor:
    service_id: str
    product_safe: bool
    development_capabilities: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()
    implementation_digest: str = ""


@dataclass(frozen=True)
class ImplementationEntry:
    descriptor: ImplementationDescriptor
    implementation: Callable | None = None
    activate: Callable[[object, tuple[str, ...]], None] | None = None


@dataclass(frozen=True)
class ImplementationCatalog:
    entries: tuple[ImplementationEntry, ...]

    def __post_init__(self) -> None:
        identities = [item.descriptor.implementation_id for item in self.entries]
        if len(set(identities)) != len(identities):
            raise ValueError("implementation IDs must be unique")

    @property
    def implementations(self) -> tuple[ImplementationDescriptor, ...]:
        return tuple(entry.descriptor for entry in self.entries)

    def implementation(self, implementation_id: str) -> Callable | None:
        for entry in self.entries:
            if entry.descriptor.implementation_id == implementation_id:
                return entry.implementation
        raise KeyError(implementation_id)


@dataclass(frozen=True)
class RuntimeServiceCatalog:
    services: tuple[RuntimeServiceDescriptor, ...] = ()

    def __post_init__(self) -> None:
        identities = [item.service_id for item in self.services]
        if len(set(identities)) != len(identities):
            raise ValueError("runtime service IDs must be unique")


@dataclass(frozen=True)
class PlanBinding:
    target: str
    implementation_id: str


@dataclass(frozen=True)
class DetachmentReport:
    reachable: tuple[str, ...]
    bindings: tuple[PlanBinding, ...]
    generated_coverage: tuple[str, ...]
    faithful_override_coverage: tuple[str, ...]
    region_replacement_coverage: tuple[str, ...]
    exe_dependent: tuple[str, ...]
    interpreter_dependent: tuple[str, ...]
    unresolved: tuple[str, ...]
    unresolved_edges: tuple[str, ...]
    required_services: tuple[str, ...]
    missing_services: tuple[str, ...]
    development_only_services: tuple[str, ...]
    policy_forbidden_services: tuple[str, ...]
    standalone_executable_ready: bool
    package_ready: bool

    def failure_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        if self.unresolved:
            lines.append("unresolved implementations: " + ", ".join(self.unresolved))
        if self.unresolved_edges:
            lines.append("unresolved control-flow edges: " + ", ".join(self.unresolved_edges))
        if self.missing_services:
            lines.append("missing runtime services: " + ", ".join(self.missing_services))
        if self.exe_dependent:
            lines.append("original-EXE-dependent: " + ", ".join(self.exe_dependent))
        if self.interpreter_dependent:
            lines.append("interpreter-dependent: " + ", ".join(self.interpreter_dependent))
        if self.development_only_services:
            lines.append(
                "development-only services: " + ", ".join(self.development_only_services)
            )
        if self.policy_forbidden_services:
            lines.append(
                "services forbidden by execution policy: "
                + ", ".join(self.policy_forbidden_services)
            )
        return tuple(lines)


@dataclass(frozen=True)
class ExecutionPlan:
    configuration: ExecutionConfiguration
    coverage_identity: str
    bindings: tuple[PlanBinding, ...]
    implementations: tuple[ImplementationDescriptor, ...]
    catalog: ImplementationCatalog
    services: tuple[RuntimeServiceDescriptor, ...]
    report: DetachmentReport
    plan_digest: str


class ExecutionPlanError(RuntimeError):
    """A strict execution profile cannot be resolved without forbidden gaps."""

    def __init__(self, profile: str, report: DetachmentReport):
        self.profile = profile
        self.report = report
        detail = "\n".join(f"  - {line}" for line in report.failure_lines())
        super().__init__(
            f"execution profile {profile!r} cannot be planned"
            + (f":\n{detail}" if detail else "")
        )


def format_execution_plan(plan: ExecutionPlan) -> str:
    """Stable, concise human report for ``play.py --plan-only`` and CI logs."""
    report = plan.report
    lines = [
        f"execution profile: {plan.configuration.profile}",
        f"program: {plan.configuration.program_identity}",
        f"plan digest: {plan.plan_digest}",
        f"reachable identities: {len(report.reachable)}",
        f"bound identities: {len(report.bindings)}",
        f"standalone executable ready: {str(report.standalone_executable_ready).lower()}",
        f"package ready: {str(report.package_ready).lower()}",
    ]
    lines.extend(f"- {failure}" for failure in report.failure_lines())
    return "\n".join(lines)


def profile_configuration(
    profile: str,
    *,
    program_identity: str,
    product_profile: str = "default",
    provider_preference: Iterable[str] = (),
    selected_overrides: Iterable[str] = (),
    build_target: BuildTarget | None = None,
) -> ExecutionConfiguration:
    """Construct one of the standard policy presets.

    Profiles populate policy axes only.  They never install implementations or
    select a backend by import order.
    """
    common = dict(
        program_identity=program_identity,
        profile=profile,
        product_profile=product_profile,
        provider_preference=tuple(provider_preference),
        selected_overrides=tuple(selected_overrides),
        build_target=build_target,
    )
    if profile == "development":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                Requirement.ALLOWED,
                Requirement.ALLOWED,
                frozenset({
                    "diagnostics", "instrumentation", "profiling", "replay",
                    "snapshots", "experimental-overrides",
                }),
            ),
        )
    if profile == "verification":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                Requirement.ALLOWED,
                Requirement.ALLOWED,
                frozenset({"diagnostics", "instrumentation", "replay", "snapshots"}),
            ),
            verification_policy=VerificationPolicy("differential", oracle_required=True),
        )
    if profile == "detached":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                Requirement.FORBIDDEN,
                Requirement.FORBIDDEN,
                frozenset({"diagnostics", "instrumentation", "replay"}),
                DynamicLoading.DECLARED_ONLY,
                strict_coverage=True,
            ),
        )
    if profile == "release":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                Requirement.FORBIDDEN,
                Requirement.FORBIDDEN,
                frozenset(),
                DynamicLoading.FORBIDDEN,
                strict_coverage=True,
            ),
        )
    raise ValueError(
        f"unknown execution profile {profile!r}; expected development, "
        "verification, detached, or release"
    )


def _compatible(implementation: ImplementationDescriptor,
                policy: ExecutionPolicy) -> bool:
    if policy.original_exe is Requirement.FORBIDDEN and implementation.requires_original_exe:
        return False
    if policy.interpreter is Requirement.FORBIDDEN and implementation.requires_interpreter:
        return False
    return True


def _ordered_candidates(
    target: str,
    implementations: tuple[ImplementationDescriptor, ...],
    preference: tuple[str, ...],
) -> list[ImplementationDescriptor]:
    rank = {implementation_id: index for index, implementation_id in enumerate(preference)}
    candidates = [item for item in implementations if target in item.targets]
    return sorted(
        candidates,
        key=lambda item: (rank.get(item.implementation_id, len(rank)), item.implementation_id),
    )


def _plan_digest(
    configuration: ExecutionConfiguration,
    coverage: ProgramCoverage,
    bindings: tuple[PlanBinding, ...],
    selected: tuple[ImplementationDescriptor, ...],
    services: tuple[RuntimeServiceDescriptor, ...],
) -> str:
    payload = {
        "program": configuration.program_identity,
        "profile": configuration.profile,
        "product": configuration.product_profile,
        "policy": {
            "exe": configuration.execution_policy.original_exe.value,
            "interpreter": configuration.execution_policy.interpreter.value,
            "capabilities": sorted(configuration.execution_policy.development_capabilities),
            "dynamic_loading": configuration.execution_policy.dynamic_loading.value,
        },
        "verification": configuration.verification_policy.mode,
        "build": (
            None if configuration.build_target is None else
            [configuration.build_target.platform, configuration.build_target.package_format]
        ),
        "coverage": {
            "identity": coverage.evidence_identity,
            "roots": list(coverage.roots),
            "reachable": sorted(coverage.reachable),
            "unresolved_edges": list(coverage.unresolved_edges),
        },
        "bindings": [[item.target, item.implementation_id] for item in bindings],
        "implementations": [
            {
                "id": item.implementation_id,
                "digest": item.implementation_digest,
                "origin": item.origin.value,
                "category": item.category.value,
                "properties": sorted(item.properties),
                "exe": item.requires_original_exe,
                "interpreter": item.requires_interpreter,
                "services": sorted(item.required_services),
                "region": item.region_id,
            }
            for item in selected
        ],
        "services": [
            {
                "id": item.service_id,
                "digest": item.implementation_digest,
                "product_safe": item.product_safe,
                "development_capabilities": sorted(item.development_capabilities),
                "dependencies": sorted(item.dependencies),
            }
            for item in services
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_execution(
    configuration: ExecutionConfiguration,
    coverage_source: CoverageSource,
    implementation_catalog: ImplementationCatalog,
    service_catalog: RuntimeServiceCatalog = RuntimeServiceCatalog(),
) -> ExecutionPlan:
    """Resolve an immutable plan or fail a strict profile before execution."""
    coverage = coverage_source.coverage_for(configuration.product_profile)
    all_implementation_items = implementation_catalog.implementations
    known_ids = {item.implementation_id for item in all_implementation_items}
    unknown_overrides = sorted(set(configuration.selected_overrides) - known_ids)
    if unknown_overrides:
        raise ValueError("unknown selected overrides: " + ", ".join(unknown_overrides))
    implementation_items = tuple(
        item for item in all_implementation_items
        if item.origin is not ImplementationOrigin.AUTHORED
        or item.implementation_id in configuration.selected_overrides
    )
    selected_override_set = set(configuration.selected_overrides)
    for target in sorted(coverage.reachable):
        owners = [
            item.implementation_id for item in implementation_items
            if target in item.targets and item.implementation_id in selected_override_set
        ]
        if len(owners) > 1:
            raise ValueError(
                f"multiple selected authored implementations own {target}: "
                + ", ".join(sorted(owners))
            )
    service_items = service_catalog.services
    service_by_id = {service.service_id: service for service in service_items}

    bindings: list[PlanBinding] = []
    selected_by_id: dict[str, ImplementationDescriptor] = {}
    unresolved: list[str] = []
    blocked_exe: list[str] = []
    blocked_interpreter: list[str] = []
    for target in sorted(coverage.reachable):
        selected_first = tuple(configuration.selected_overrides) + tuple(
            item for item in configuration.provider_preference
            if item not in configuration.selected_overrides
        )
        candidates = _ordered_candidates(
            target, implementation_items, selected_first
        )
        selected = next(
            (candidate for candidate in candidates
             if _compatible(candidate, configuration.execution_policy)),
            None,
        )
        if selected is None:
            unresolved.append(target)
            if any(candidate.requires_original_exe for candidate in candidates):
                blocked_exe.append(target)
            if any(candidate.requires_interpreter for candidate in candidates):
                blocked_interpreter.append(target)
            continue
        bindings.append(PlanBinding(target, selected.implementation_id))
        selected_by_id[selected.implementation_id] = selected

    selected = tuple(sorted(selected_by_id.values(), key=lambda item: item.implementation_id))
    required_service_set = {
        service_id for item in selected for service_id in item.required_services
    }
    pending_services = list(required_service_set)
    while pending_services:
        service_id = pending_services.pop()
        service = service_by_id.get(service_id)
        if service is None:
            continue
        for dependency in service.dependencies:
            if dependency not in required_service_set:
                required_service_set.add(dependency)
                pending_services.append(dependency)
    required_service_ids = sorted(required_service_set)
    missing_services = tuple(
        service_id for service_id in required_service_ids if service_id not in service_by_id
    )
    selected_services = tuple(
        service_by_id[service_id]
        for service_id in required_service_ids
        if service_id in service_by_id
    )
    development_only_services = tuple(sorted(
        service.service_id for service in selected_services if not service.product_safe
    ))
    policy_forbidden_services = tuple(sorted(
        service.service_id for service in selected_services
        if not service.development_capabilities.issubset(
            configuration.execution_policy.development_capabilities
        )
    ))

    exe_dependent = tuple(sorted(set(blocked_exe) | {
        target
        for target, implementation_id in ((b.target, b.implementation_id) for b in bindings)
        if selected_by_id[implementation_id].requires_original_exe
    }))
    interpreter_dependent = tuple(sorted(set(blocked_interpreter) | {
        target
        for target, implementation_id in ((b.target, b.implementation_id) for b in bindings)
        if selected_by_id[implementation_id].requires_interpreter
    }))
    generated = tuple(sorted(
        binding.target for binding in bindings
        if selected_by_id[binding.implementation_id].origin is ImplementationOrigin.GENERATED
    ))
    faithful = tuple(sorted(
        binding.target for binding in bindings
        if selected_by_id[binding.implementation_id].category is OverrideCategory.FAITHFUL
    ))
    regions = tuple(sorted({
        item.region_id for item in selected if item.region_id is not None
    }))
    standalone_ready = not (
        unresolved or coverage.unresolved_edges or missing_services
        or exe_dependent or interpreter_dependent
    )
    package_ready = (
        standalone_ready
        and not development_only_services
        and not policy_forbidden_services
        and configuration.build_target is not None
    )
    binding_items = tuple(bindings)
    report = DetachmentReport(
        reachable=tuple(sorted(coverage.reachable)),
        bindings=binding_items,
        generated_coverage=generated,
        faithful_override_coverage=faithful,
        region_replacement_coverage=regions,
        exe_dependent=exe_dependent,
        interpreter_dependent=interpreter_dependent,
        unresolved=tuple(unresolved),
        unresolved_edges=tuple(sorted(coverage.unresolved_edges)),
        required_services=tuple(required_service_ids),
        missing_services=missing_services,
        development_only_services=development_only_services,
        policy_forbidden_services=policy_forbidden_services,
        standalone_executable_ready=standalone_ready,
        package_ready=package_ready,
    )

    policy = configuration.execution_policy
    policy_failure = (
        (policy.original_exe is Requirement.FORBIDDEN and bool(exe_dependent))
        or (policy.interpreter is Requirement.FORBIDDEN and bool(interpreter_dependent))
        or bool(unresolved)
        or bool(missing_services)
        or bool(policy_forbidden_services)
        or (policy.strict_coverage and bool(coverage.unresolved_edges))
        or (
            configuration.profile == "release"
            and bool(development_only_services)
        )
    )
    if policy_failure:
        raise ExecutionPlanError(configuration.profile, report)

    digest = _plan_digest(
        configuration, coverage, binding_items, selected, selected_services
    )
    return ExecutionPlan(
        configuration=configuration,
        coverage_identity=coverage.evidence_identity,
        bindings=binding_items,
        implementations=selected,
        catalog=implementation_catalog,
        services=selected_services,
        report=report,
        plan_digest=digest,
    )
