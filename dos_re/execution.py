"""Execution composition and dependency-policy planning for dos_re 3.0.

This module is deliberately backend-neutral.  It does not import a CPU,
interpreter, player, replay implementation, or the future Execution Atlas.
Ports describe coverage, implementations, and services as immutable records;
the planner selects one implementation per reachable identity and fails before
runtime construction when a strict profile cannot be satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable, Protocol


class Requirement(str, Enum):
    """Whether an execution dependency is required, permitted, or forbidden."""

    REQUIRED = "required"
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"


class DependencyCapability(str, Enum):
    """Stable names for independently detachable runtime dependencies.

    Descriptors may also use project-defined string capabilities.  These core
    names are shared by the planner, exporter, documentation, and future
    Execution Atlas.
    """

    ORIGINAL_EXE = "original-exe"
    ORIGINAL_CODE = "original-code"
    INTERPRETER = "interpreter"
    CPU_MODEL = "cpu-model"
    DOS_MEMORY = "dos-memory"
    DOS_SERVICES = "dos-services"
    DOS_RE_RUNTIME = "dos-re-runtime"
    ORACLE = "oracle"
    REPLAY = "replay"
    SNAPSHOTS = "snapshots"
    DIAGNOSTICS = "diagnostics"
    INSTRUMENTATION = "instrumentation"
    PROFILING = "profiling"
    EXPERIMENTAL_OVERRIDES = "experimental-overrides"
    DEVELOPMENT_TOOLING = "development-tooling"


def capability_name(capability: str | DependencyCapability) -> str:
    return capability.value if isinstance(capability, DependencyCapability) else capability


@dataclass(frozen=True)
class CapabilityPolicy:
    capability: str
    requirement: Requirement


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
    capabilities: tuple[CapabilityPolicy, ...] = ()
    dynamic_loading: DynamicLoading = DynamicLoading.ALLOWED
    strict_coverage: bool = False

    def __post_init__(self) -> None:
        names = [item.capability for item in self.capabilities]
        if len(set(names)) != len(names):
            raise ValueError("execution capability policies must be unique")

    def requirement_for(self, capability: str | DependencyCapability) -> Requirement:
        name = capability_name(capability)
        for item in self.capabilities:
            if item.capability == name:
                return item.requirement
        return Requirement.ALLOWED

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset(
            item.capability for item in self.capabilities
            if item.requirement is Requirement.REQUIRED
        )

    @property
    def forbidden_capabilities(self) -> frozenset[str]:
        return frozenset(
            item.capability for item in self.capabilities
            if item.requirement is Requirement.FORBIDDEN
        )


@dataclass(frozen=True)
class VerificationPolicy:
    mode: str = "none"
    oracle_required: bool = False


@dataclass(frozen=True)
class BuildTarget:
    platform: str
    package_format: str = ""


class BootstrapExportMode(str, Enum):
    INCLUDE = "include"
    GENERATE = "generate"


@dataclass(frozen=True)
class BootstrapArtifact:
    """One file a bootstrap provider needs in the packaged runtime."""

    artifact_id: str
    runtime_path: str
    source_path: str = ""
    export_mode: BootstrapExportMode = BootstrapExportMode.INCLUDE
    expected_sha256: str = ""
    generation_instruction: str = ""
    materializer: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class BootstrapProvider:
    """Declared source of the initial continuation/runtime state."""

    provider_id: str
    state_outputs: tuple[str, ...]
    artifacts: tuple[BootstrapArtifact, ...] = ()
    build_required_capabilities: frozenset[str] = frozenset()
    runtime_required_capabilities: frozenset[str] = frozenset()
    initialized_capabilities: frozenset[str] = frozenset()
    required_services: frozenset[str] = frozenset()
    valid_profiles: frozenset[str] = frozenset({
        "development", "verification", "detached", "release",
    })
    provider_digest: str = ""

    @property
    def kind(self) -> str:
        return "bootstrap"

    def components(self) -> tuple["BootstrapProvider", ...]:
        return (self,)


@dataclass(frozen=True)
class ExeBootstrapProvider(BootstrapProvider):
    """Load initial state by executing/loading the original program at runtime."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "build_required_capabilities",
            self.build_required_capabilities
            | {DependencyCapability.ORIGINAL_EXE.value},
        )
        object.__setattr__(
            self,
            "runtime_required_capabilities",
            self.runtime_required_capabilities
            | {
                DependencyCapability.ORIGINAL_EXE.value,
                DependencyCapability.ORIGINAL_CODE.value,
            },
        )

    @property
    def kind(self) -> str:
        return "exe"


@dataclass(frozen=True)
class BuildImageBootstrapProvider(BootstrapProvider):
    """Restore a generated/packageable state image without its build inputs."""

    @property
    def kind(self) -> str:
        return "build-image"


@dataclass(frozen=True)
class NativeBootstrapProvider(BootstrapProvider):
    """Construct initial state directly in Python or native product code."""

    @property
    def kind(self) -> str:
        return "native"


@dataclass(frozen=True)
class CompositeBootstrapProvider(BootstrapProvider):
    """Combine image, asset, device, and native-state bootstrap providers."""

    providers: tuple[BootstrapProvider, ...] = ()

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("composite bootstrap provider requires components")

    @property
    def kind(self) -> str:
        return "composite"

    def components(self) -> tuple[BootstrapProvider, ...]:
        items: list[BootstrapProvider] = [self]
        for provider in self.providers:
            items.extend(provider.components())
        return tuple(items)


def default_bootstrap_provider() -> BootstrapProvider:
    """State-less default for planner-only/library compositions."""
    return NativeBootstrapProvider(
        provider_id="native-empty",
        state_outputs=("caller-owned initial state",),
        provider_digest="dos-re-native-empty-v1",
    )


@dataclass(frozen=True)
class ExecutionConfiguration:
    program_identity: str
    profile: str
    product_profile: str
    execution_policy: ExecutionPolicy
    bootstrap_provider: BootstrapProvider
    verification_policy: VerificationPolicy = VerificationPolicy()
    provider_preference: tuple[str, ...] = ()
    selected_overrides: tuple[str, ...] = ()
    product_services: frozenset[str] = frozenset()
    requested_capabilities: frozenset[str] = frozenset()
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
    required_capabilities: frozenset[str] = frozenset()
    required_services: frozenset[str] = frozenset()
    required_assets: frozenset[str] = frozenset()
    supported_platforms: frozenset[str] = frozenset()
    verification_evidence: frozenset[str] = frozenset()
    implementation_digest: str = ""
    region_id: str | None = None


@dataclass(frozen=True)
class RuntimeServiceDescriptor:
    service_id: str
    product_safe: bool
    required_capabilities: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()
    required_assets: frozenset[str] = frozenset()
    supported_platforms: frozenset[str] = frozenset()
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
class CapabilityUse:
    capability: str
    consumers: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityBlocker:
    capability: str
    target: str
    implementation_id: str
    alternatives_without_capability: tuple[str, ...]


@dataclass(frozen=True)
class DetachmentMilestone:
    capability: str
    detached: bool
    dependency_group: tuple[str, ...] = ()


@dataclass(frozen=True)
class BootstrapArtifactStatus:
    artifact_id: str
    runtime_path: str
    source_path: str
    export_mode: BootstrapExportMode
    materializable: bool
    generation_instruction: str


@dataclass(frozen=True)
class DetachmentReport:
    reachable: tuple[str, ...]
    bindings: tuple[PlanBinding, ...]
    generated_coverage: tuple[str, ...]
    faithful_override_coverage: tuple[str, ...]
    region_replacement_coverage: tuple[str, ...]
    unresolved: tuple[str, ...]
    unresolved_edges: tuple[str, ...]
    required_services: tuple[str, ...]
    missing_services: tuple[str, ...]
    required_assets: tuple[str, ...]
    packaging_incompatible: tuple[str, ...]
    development_only_services: tuple[str, ...]
    policy_forbidden_services: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    capability_uses: tuple[CapabilityUse, ...]
    capability_blockers: tuple[CapabilityBlocker, ...]
    policy_forbidden_capabilities: tuple[str, ...]
    detachment_milestones: tuple[DetachmentMilestone, ...]
    bootstrap_provider_id: str
    bootstrap_kind: str
    bootstrap_state_outputs: tuple[str, ...]
    bootstrap_build_capabilities: tuple[str, ...]
    bootstrap_runtime_capabilities: tuple[str, ...]
    bootstrap_initialized_capabilities: tuple[str, ...]
    bootstrap_artifacts: tuple[BootstrapArtifactStatus, ...]
    missing_bootstrap_artifacts: tuple[str, ...]
    bootstrap_profile_valid: bool
    package_ready: bool

    def requires(self, capability: str | DependencyCapability) -> bool:
        return capability_name(capability) in self.required_capabilities

    def is_detached_from(self, capability: str | DependencyCapability) -> bool:
        name = capability_name(capability)
        for item in self.detachment_milestones:
            if item.capability == name:
                return item.detached
        return name not in self.required_capabilities

    def failure_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        if self.unresolved:
            lines.append("unresolved implementations: " + ", ".join(self.unresolved))
        if self.unresolved_edges:
            lines.append("unresolved control-flow edges: " + ", ".join(self.unresolved_edges))
        if self.missing_services:
            lines.append("missing runtime services: " + ", ".join(self.missing_services))
        if self.packaging_incompatible:
            lines.append(
                "not compatible with build target: "
                + ", ".join(self.packaging_incompatible)
            )
        if self.policy_forbidden_capabilities:
            lines.append(
                "required capabilities forbidden by execution policy: "
                + ", ".join(self.policy_forbidden_capabilities)
            )
        if not self.bootstrap_profile_valid:
            lines.append(
                f"bootstrap provider {self.bootstrap_provider_id!r} is not valid "
                "for this execution profile"
            )
        if self.missing_bootstrap_artifacts:
            lines.append(
                "missing bootstrap artifacts: "
                + ", ".join(self.missing_bootstrap_artifacts)
            )
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
    bootstrap_provider: BootstrapProvider
    coverage_identity: str
    bindings: tuple[PlanBinding, ...]
    implementations: tuple[ImplementationDescriptor, ...]
    catalog: ImplementationCatalog
    services: tuple[RuntimeServiceDescriptor, ...]
    report: DetachmentReport
    plan_digest: str

    def bootstrap_artifact_paths(
        self,
        *,
        packaged: bool = False,
        root: str | Path | None = None,
    ) -> dict[str, Path]:
        """Resolve selected bootstrap artifacts for a backend launch."""
        base = Path(root) if root is not None else Path()
        paths: dict[str, Path] = {}
        for provider in self.bootstrap_provider.components():
            for artifact in provider.artifacts:
                value = artifact.runtime_path if packaged else artifact.source_path
                if not value:
                    raise RuntimeCapabilityViolation(
                        f"bootstrap-artifact:{artifact.artifact_id}",
                        f"bootstrap:{self.bootstrap_provider.provider_id}",
                        self.plan_digest,
                    )
                path = Path(value)
                paths[artifact.artifact_id] = (
                    base / path if packaged and not path.is_absolute() else path
                )
        return paths

    def require_capability(
        self,
        capability: str | DependencyCapability,
        *,
        consumer: str,
    ) -> None:
        """Fail loudly when runtime code requests an undeclared dependency."""
        name = capability_name(capability)
        if name not in self.report.required_capabilities:
            raise RuntimeCapabilityViolation(name, consumer, self.plan_digest)


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


class RuntimeCapabilityViolation(RuntimeError):
    """Runtime code requested a dependency outside the immutable plan closure."""

    def __init__(self, capability: str, consumer: str, plan_digest: str):
        self.capability = capability
        self.consumer = consumer
        self.plan_digest = plan_digest
        super().__init__(
            f"{consumer} requested capability {capability!r}, which is outside "
            f"execution plan {plan_digest[:12]} dependency closure"
        )


def format_execution_plan(plan: ExecutionPlan) -> str:
    """Stable, concise human report for ``play.py --plan-only`` and CI logs."""
    report = plan.report
    lines = [
        f"execution profile: {plan.configuration.profile}",
        f"program: {plan.configuration.program_identity}",
        f"plan digest: {plan.plan_digest}",
        f"bootstrap provider: {report.bootstrap_provider_id} ({report.bootstrap_kind})",
        f"reachable identities: {len(report.reachable)}",
        f"bound identities: {len(report.bindings)}",
        "required capabilities: "
        + (", ".join(report.required_capabilities) or "none"),
        f"package ready: {str(report.package_ready).lower()}",
    ]
    lines.extend(
        f"{item.capability} detached: {str(item.detached).lower()}"
        for item in report.detachment_milestones
    )
    lines.extend(f"- {failure}" for failure in report.failure_lines())
    return "\n".join(lines)


def profile_configuration(
    profile: str,
    *,
    program_identity: str,
    product_profile: str = "default",
    provider_preference: Iterable[str] = (),
    selected_overrides: Iterable[str] = (),
    product_services: Iterable[str] = (),
    requested_capabilities: Iterable[str] = (),
    bootstrap_provider: BootstrapProvider | None = None,
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
        product_services=frozenset(product_services),
        requested_capabilities=frozenset(requested_capabilities),
        bootstrap_provider=bootstrap_provider or default_bootstrap_provider(),
        build_target=build_target,
    )
    development = (
        CapabilityPolicy(DependencyCapability.ORIGINAL_EXE.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.ORIGINAL_CODE.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.INTERPRETER.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.ORACLE.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.REPLAY.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.SNAPSHOTS.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.DIAGNOSTICS.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.INSTRUMENTATION.value, Requirement.ALLOWED),
        CapabilityPolicy(DependencyCapability.PROFILING.value, Requirement.ALLOWED),
        CapabilityPolicy(
            DependencyCapability.EXPERIMENTAL_OVERRIDES.value, Requirement.ALLOWED
        ),
        CapabilityPolicy(DependencyCapability.DEVELOPMENT_TOOLING.value, Requirement.ALLOWED),
    )
    detached = (
        CapabilityPolicy(DependencyCapability.ORIGINAL_EXE.value, Requirement.FORBIDDEN),
        CapabilityPolicy(DependencyCapability.ORIGINAL_CODE.value, Requirement.FORBIDDEN),
        CapabilityPolicy(DependencyCapability.INTERPRETER.value, Requirement.FORBIDDEN),
        CapabilityPolicy(DependencyCapability.ORACLE.value, Requirement.FORBIDDEN),
        CapabilityPolicy(DependencyCapability.PROFILING.value, Requirement.FORBIDDEN),
        CapabilityPolicy(
            DependencyCapability.EXPERIMENTAL_OVERRIDES.value, Requirement.FORBIDDEN
        ),
        CapabilityPolicy(
            DependencyCapability.DEVELOPMENT_TOOLING.value, Requirement.FORBIDDEN
        ),
    )
    release = tuple(
        CapabilityPolicy(item.value, Requirement.FORBIDDEN)
        for item in (
            DependencyCapability.ORIGINAL_EXE,
            DependencyCapability.ORIGINAL_CODE,
            DependencyCapability.INTERPRETER,
            DependencyCapability.ORACLE,
            DependencyCapability.REPLAY,
            DependencyCapability.SNAPSHOTS,
            DependencyCapability.DIAGNOSTICS,
            DependencyCapability.INSTRUMENTATION,
            DependencyCapability.PROFILING,
            DependencyCapability.EXPERIMENTAL_OVERRIDES,
            DependencyCapability.DEVELOPMENT_TOOLING,
        )
    )
    if profile == "development":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                capabilities=development,
            ),
        )
    if profile == "verification":
        verification_capabilities = tuple(
            CapabilityPolicy(
                item.capability,
                Requirement.REQUIRED
                if item.capability == DependencyCapability.ORACLE.value
                else item.requirement,
            )
            for item in development
        )
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                capabilities=verification_capabilities,
            ),
            verification_policy=VerificationPolicy("differential", oracle_required=True),
        )
    if profile == "detached":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                capabilities=detached,
                dynamic_loading=DynamicLoading.DECLARED_ONLY,
                strict_coverage=True,
            ),
        )
    if profile == "release":
        return ExecutionConfiguration(
            **common,
            execution_policy=ExecutionPolicy(
                capabilities=release,
                dynamic_loading=DynamicLoading.FORBIDDEN,
                strict_coverage=True,
            ),
        )
    raise ValueError(
        f"unknown execution profile {profile!r}; expected development, "
        "verification, detached, or release"
    )


def _compatible(
    implementation: ImplementationDescriptor,
    policy: ExecutionPolicy,
    build_target: BuildTarget | None,
) -> bool:
    capability_compatible = not (
        implementation.required_capabilities & policy.forbidden_capabilities
    )
    target_compatible = (
        build_target is None
        or not implementation.supported_platforms
        or build_target.platform in implementation.supported_platforms
    )
    return capability_compatible and target_compatible


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
    bootstrap = configuration.bootstrap_provider
    bootstrap_components = bootstrap.components()
    payload = {
        "program": configuration.program_identity,
        "profile": configuration.profile,
        "product": configuration.product_profile,
        "policy": {
            "capabilities": sorted(
                (item.capability, item.requirement.value)
                for item in configuration.execution_policy.capabilities
            ),
            "dynamic_loading": configuration.execution_policy.dynamic_loading.value,
        },
        "product_services": sorted(configuration.product_services),
        "requested_capabilities": sorted(configuration.requested_capabilities),
        "bootstrap": {
            "selected": bootstrap.provider_id,
            "kind": bootstrap.kind,
            "components": [
                {
                    "id": item.provider_id,
                    "kind": item.kind,
                    "outputs": list(item.state_outputs),
                    "build_capabilities": sorted(
                        item.build_required_capabilities
                    ),
                    "runtime_capabilities": sorted(
                        item.runtime_required_capabilities
                    ),
                    "initialized_capabilities": sorted(
                        item.initialized_capabilities
                    ),
                    "services": sorted(item.required_services),
                    "profiles": sorted(item.valid_profiles),
                    "digest": item.provider_digest,
                    "artifacts": [
                        {
                            "id": artifact.artifact_id,
                            "runtime_path": artifact.runtime_path,
                            "source_path": artifact.source_path,
                            "mode": artifact.export_mode.value,
                            "sha256": artifact.expected_sha256,
                            "instruction": artifact.generation_instruction,
                        }
                        for artifact in item.artifacts
                    ],
                }
                for item in bootstrap_components
            ],
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
                "capabilities": sorted(item.required_capabilities),
                "services": sorted(item.required_services),
                "assets": sorted(item.required_assets),
                "platforms": sorted(item.supported_platforms),
                "verification_evidence": sorted(item.verification_evidence),
                "region": item.region_id,
            }
            for item in selected
        ],
        "services": [
            {
                "id": item.service_id,
                "digest": item.implementation_digest,
                "product_safe": item.product_safe,
                "capabilities": sorted(item.required_capabilities),
                "dependencies": sorted(item.dependencies),
                "assets": sorted(item.required_assets),
                "platforms": sorted(item.supported_platforms),
            }
            for item in services
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bootstrap_status(
    provider: BootstrapProvider,
    profile: str,
) -> tuple[
    tuple[BootstrapProvider, ...],
    tuple[BootstrapArtifactStatus, ...],
    tuple[str, ...],
    bool,
]:
    components = provider.components()
    provider_ids = [item.provider_id for item in components]
    if len(set(provider_ids)) != len(provider_ids):
        raise ValueError("bootstrap provider component IDs must be unique")
    artifacts = tuple(
        artifact for item in components for artifact in item.artifacts
    )
    artifact_ids = [item.artifact_id for item in artifacts]
    runtime_paths = [Path(item.runtime_path).as_posix() for item in artifacts]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("bootstrap artifact IDs must be unique")
    if len(set(runtime_paths)) != len(runtime_paths):
        raise ValueError("bootstrap artifact runtime paths must be unique")
    statuses: list[BootstrapArtifactStatus] = []
    missing: list[str] = []
    for artifact in artifacts:
        source = Path(artifact.source_path) if artifact.source_path else None
        materializable = False
        reason = ""
        if artifact.export_mode is BootstrapExportMode.INCLUDE:
            if source is None or not source.is_file():
                reason = f"{artifact.artifact_id} ({artifact.source_path or 'no source path'})"
            elif artifact.expected_sha256:
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest != artifact.expected_sha256:
                    reason = (
                        f"{artifact.artifact_id} (hash mismatch at {source})"
                    )
                else:
                    materializable = True
            else:
                materializable = True
        elif artifact.materializer is None:
            reason = f"{artifact.artifact_id} (no export materializer)"
        else:
            materializable = True
        if reason:
            if artifact.generation_instruction:
                reason += f"; {artifact.generation_instruction}"
            missing.append(reason)
        statuses.append(BootstrapArtifactStatus(
            artifact_id=artifact.artifact_id,
            runtime_path=Path(artifact.runtime_path).as_posix(),
            source_path=artifact.source_path,
            export_mode=artifact.export_mode,
            materializable=materializable,
            generation_instruction=artifact.generation_instruction,
        ))
    profile_valid = all(profile in item.valid_profiles for item in components)
    return components, tuple(statuses), tuple(missing), profile_valid


def plan_execution(
    configuration: ExecutionConfiguration,
    coverage_source: CoverageSource,
    implementation_catalog: ImplementationCatalog,
    service_catalog: RuntimeServiceCatalog = RuntimeServiceCatalog(),
) -> ExecutionPlan:
    """Resolve an immutable plan or fail a strict profile before execution."""
    (
        bootstrap_components,
        bootstrap_artifacts,
        missing_bootstrap_artifacts,
        bootstrap_profile_valid,
    ) = _bootstrap_status(
        configuration.bootstrap_provider,
        configuration.profile,
    )
    bootstrap_build_capabilities = frozenset(
        capability
        for provider in bootstrap_components
        for capability in provider.build_required_capabilities
    )
    bootstrap_runtime_capabilities = frozenset(
        capability
        for provider in bootstrap_components
        for capability in provider.runtime_required_capabilities
    )
    bootstrap_initialized_capabilities = frozenset(
        capability
        for provider in bootstrap_components
        for capability in provider.initialized_capabilities
    )
    bootstrap_services = frozenset(
        service
        for provider in bootstrap_components
        for service in provider.required_services
    )
    bootstrap_state_outputs = tuple(sorted({
        output
        for provider in bootstrap_components
        for output in provider.state_outputs
    }))
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
    blocked_capabilities: dict[str, set[str]] = {}
    packaging_incompatible: list[str] = []
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
             if _compatible(
                 candidate,
                 configuration.execution_policy,
                 configuration.build_target,
             )),
            None,
        )
        if selected is None:
            unresolved.append(target)
            for candidate in candidates:
                for capability in (
                    candidate.required_capabilities
                    & configuration.execution_policy.forbidden_capabilities
                ):
                    blocked_capabilities.setdefault(capability, set()).add(
                        f"target:{target}"
                    )
                if (
                    configuration.build_target is not None
                    and candidate.supported_platforms
                    and configuration.build_target.platform
                    not in candidate.supported_platforms
                ):
                    packaging_incompatible.append(
                        f"{target}:{candidate.implementation_id}"
                    )
            continue
        bindings.append(PlanBinding(target, selected.implementation_id))
        selected_by_id[selected.implementation_id] = selected

    selected = tuple(sorted(selected_by_id.values(), key=lambda item: item.implementation_id))
    required_service_set = (
        set(configuration.product_services)
        | set(bootstrap_services)
        | {
        service_id for item in selected for service_id in item.required_services
        }
    )
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
    required_assets = tuple(sorted(
        {
            asset
            for item in selected
            for asset in item.required_assets
        }
        | {
            asset
            for service in selected_services
            for asset in service.required_assets
        }
    ))
    incompatible_services = tuple(sorted(
        f"service:{service.service_id}"
        for service in selected_services
        if configuration.build_target is not None
        and service.supported_platforms
        and configuration.build_target.platform not in service.supported_platforms
    ))
    packaging_incompatible_items = tuple(sorted(
        set(packaging_incompatible) | set(incompatible_services)
    ))
    development_only_services = tuple(sorted(
        service.service_id for service in selected_services if not service.product_safe
    ))
    capability_consumers: dict[str, set[str]] = {}
    for item in selected:
        for capability in item.required_capabilities:
            capability_consumers.setdefault(capability, set()).add(
                f"implementation:{item.implementation_id}"
            )
    for service in selected_services:
        for capability in service.required_capabilities:
            capability_consumers.setdefault(capability, set()).add(
                f"service:{service.service_id}"
            )
    for capability in configuration.execution_policy.required_capabilities:
        capability_consumers.setdefault(capability, set()).add(
            f"policy:{configuration.profile}"
        )
    for capability in configuration.requested_capabilities:
        capability_consumers.setdefault(capability, set()).add("configuration")
    for provider in bootstrap_components:
        for capability in provider.runtime_required_capabilities:
            capability_consumers.setdefault(capability, set()).add(
                f"bootstrap:{provider.provider_id}"
            )
    for capability, consumers in blocked_capabilities.items():
        capability_consumers.setdefault(capability, set()).update(consumers)
    required_capability_set = (
        set(configuration.execution_policy.required_capabilities)
        | set(configuration.requested_capabilities)
        | set(bootstrap_runtime_capabilities)
        | set(blocked_capabilities)
        | {
            capability
            for item in selected
            for capability in item.required_capabilities
        }
        | {
            capability
            for service in selected_services
            for capability in service.required_capabilities
        }
    )
    forbidden_capability_set = (
        required_capability_set
        & configuration.execution_policy.forbidden_capabilities
    ) | set(blocked_capabilities)
    policy_forbidden_capabilities = tuple(sorted(forbidden_capability_set))
    policy_forbidden_services = tuple(sorted(
        service.service_id for service in selected_services
        if service.required_capabilities
        & configuration.execution_policy.forbidden_capabilities
    ))
    capability_uses = tuple(
        CapabilityUse(capability, tuple(sorted(consumers)))
        for capability, consumers in sorted(capability_consumers.items())
    )
    capability_blockers = tuple(
        CapabilityBlocker(
            capability=capability,
            target=binding.target,
            implementation_id=binding.implementation_id,
            alternatives_without_capability=tuple(sorted(
                candidate.implementation_id
                for candidate in all_implementation_items
                if binding.target in candidate.targets
                and capability not in candidate.required_capabilities
            )),
        )
        for binding in bindings
        for capability in sorted(
            selected_by_id[binding.implementation_id].required_capabilities
        )
    )
    milestone_groups = (
        (
            DependencyCapability.ORIGINAL_EXE.value,
            (
                DependencyCapability.ORIGINAL_EXE.value,
                DependencyCapability.ORIGINAL_CODE.value,
            ),
        ),
        (
            DependencyCapability.INTERPRETER.value,
            (DependencyCapability.INTERPRETER.value,),
        ),
        (
            DependencyCapability.CPU_MODEL.value,
            (DependencyCapability.CPU_MODEL.value,),
        ),
        (
            DependencyCapability.DOS_MEMORY.value,
            (DependencyCapability.DOS_MEMORY.value,),
        ),
        (
            DependencyCapability.DOS_SERVICES.value,
            (DependencyCapability.DOS_SERVICES.value,),
        ),
        (
            DependencyCapability.DOS_RE_RUNTIME.value,
            (DependencyCapability.DOS_RE_RUNTIME.value,),
        ),
    )
    milestones = tuple(
        DetachmentMilestone(
            name,
            not bool(required_capability_set & set(group)),
            group,
        )
        for name, group in milestone_groups
    )
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
    package_ready = (
        not unresolved
        and not coverage.unresolved_edges
        and not missing_services
        and not packaging_incompatible_items
        and bootstrap_profile_valid
        and not missing_bootstrap_artifacts
        and not policy_forbidden_capabilities
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
        unresolved=tuple(unresolved),
        unresolved_edges=tuple(sorted(coverage.unresolved_edges)),
        required_services=tuple(required_service_ids),
        missing_services=missing_services,
        required_assets=required_assets,
        packaging_incompatible=packaging_incompatible_items,
        development_only_services=development_only_services,
        policy_forbidden_services=policy_forbidden_services,
        required_capabilities=tuple(sorted(required_capability_set)),
        capability_uses=capability_uses,
        capability_blockers=capability_blockers,
        policy_forbidden_capabilities=policy_forbidden_capabilities,
        detachment_milestones=milestones,
        bootstrap_provider_id=configuration.bootstrap_provider.provider_id,
        bootstrap_kind=configuration.bootstrap_provider.kind,
        bootstrap_state_outputs=bootstrap_state_outputs,
        bootstrap_build_capabilities=tuple(sorted(
            bootstrap_build_capabilities
        )),
        bootstrap_runtime_capabilities=tuple(sorted(
            bootstrap_runtime_capabilities
        )),
        bootstrap_initialized_capabilities=tuple(sorted(
            bootstrap_initialized_capabilities
        )),
        bootstrap_artifacts=bootstrap_artifacts,
        missing_bootstrap_artifacts=missing_bootstrap_artifacts,
        bootstrap_profile_valid=bootstrap_profile_valid,
        package_ready=package_ready,
    )

    policy = configuration.execution_policy
    policy_failure = (
        bool(policy_forbidden_capabilities)
        or bool(unresolved)
        or bool(missing_services)
        or bool(packaging_incompatible_items)
        or not bootstrap_profile_valid
        or bool(missing_bootstrap_artifacts)
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
        bootstrap_provider=configuration.bootstrap_provider,
        coverage_identity=coverage.evidence_identity,
        bindings=binding_items,
        implementations=selected,
        catalog=implementation_catalog,
        services=selected_services,
        report=report,
        plan_digest=digest,
    )
