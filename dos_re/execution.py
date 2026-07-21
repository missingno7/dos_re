"""Execution composition and dependency-policy planning for dos_re 3.0.

This module is deliberately backend-neutral. It does not import a CPU,
interpreter, player, replay implementation, or Execution Atlas storage.
Ports describe coverage, implementations, and services as immutable records;
the planner selects one implementation per reachable identity and fails before
runtime construction when a strict profile cannot be satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
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


class RecoveryLevel(str, Enum):
    """How an implementation realizes original program semantics.

    Levels describe an individual implementation, never a whole-game mode.
    A single plan may select implementations at every level below.
    """

    INTERPRETED = "interpreted"
    GENERATED_VMLESS = "generated-vmless"
    GENERATED_CPULESS = "generated-cpuless"
    GENERATED_ABI = "generated-abi"
    AUTHORED_NATIVE = "authored-native"


class EvidenceGrade(IntEnum):
    """Finite evidence attached to an implementation candidate.

    ``REPLAY_CORPUS`` means the declared replay corpus passed; it deliberately
    does not claim universal correctness.  Product policy chooses the minimum
    acceptable grade and can relax it during development.
    """

    NONE = 0
    FOCUSED = 1
    REPLAY_CORPUS = 2
    EXHAUSTIVE = 3


class OverrideCategory(str, Enum):
    BASELINE = "baseline"
    FAITHFUL = "faithful"
    ENHANCEMENT = "enhancement"
    BEHAVIORAL = "behavioral"
    INSTRUMENTATION = "instrumentation"


@dataclass(frozen=True)
class ImplementationContract:
    """Backend-neutral contract shared by generated and authored candidates."""

    contract_id: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    observable_effects: tuple[str, ...] = ()
    state_authority: str = "continuation-state"
    preservation: tuple[str, ...] = ()


class RegionStateOwnership(str, Enum):
    """Where authoritative state lives while an execution island is active."""

    SHARED_DOS_MEMORY = "shared-dos-memory"
    NATIVE_STATE = "native-state"
    IMPORTED_NATIVE_STATE = "imported-native-state"


@dataclass(frozen=True)
class RegionEntryPoint:
    """A stable original-program point which may enter an execution island."""

    entry_id: str
    target: str

    def __post_init__(self) -> None:
        if not self.entry_id or not self.target:
            raise ValueError("region entry ID and target must not be empty")


@dataclass(frozen=True)
class RegionExitPoint:
    """One named island outcome and its surrounding-program continuation."""

    exit_id: str
    continuation: str

    def __post_init__(self) -> None:
        if not self.exit_id or not self.continuation:
            raise ValueError("region exit ID and continuation must not be empty")


@dataclass(frozen=True)
class ExecutionRegionContract:
    """Long-lived ownership contract for a replaceable execution region.

    ``covered_targets`` are contextual: their ordinary plan bindings remain
    valid for calls made outside the island, but are dormant while this region
    owns control.  This is what lets a large island collapse its internal hook
    seams without incorrectly claiming every invocation of a shared function.
    """

    region_id: str
    carrier_id: str
    state_ownership: RegionStateOwnership
    entries: tuple[RegionEntryPoint, ...]
    exits: tuple[RegionExitPoint, ...]
    covered_targets: frozenset[str] = frozenset()
    replay_boundaries: frozenset[str] = frozenset()
    state_inputs: tuple[str, ...] = ()
    state_outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.region_id or not self.carrier_id:
            raise ValueError("region and carrier IDs must not be empty")
        if not self.entries:
            raise ValueError("execution regions require at least one entry")
        if not self.exits:
            raise ValueError("execution regions require at least one exit")
        entry_ids = [item.entry_id for item in self.entries]
        entry_targets = [item.target for item in self.entries]
        exit_ids = [item.exit_id for item in self.exits]
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("region entry IDs must be unique")
        if len(set(entry_targets)) != len(entry_targets):
            raise ValueError("region entry targets must be unique")
        if len(set(exit_ids)) != len(exit_ids):
            raise ValueError("region exit IDs must be unique")


class FeatureCategory(str, Enum):
    PRESENTATION = "presentation"
    BEHAVIORAL = "behavioral"
    INSTRUMENTATION = "instrumentation"


@dataclass(frozen=True)
class ExecutionPolicy:
    capabilities: tuple[CapabilityPolicy, ...] = ()
    dynamic_loading: DynamicLoading = DynamicLoading.ALLOWED
    strict_coverage: bool = False
    minimum_authored_evidence: EvidenceGrade = EvidenceGrade.NONE

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
    enabled_features: tuple[str, ...] = ()
    product_services: frozenset[str] = frozenset()
    requested_capabilities: frozenset[str] = frozenset()
    build_target: BuildTarget | None = None

    def __post_init__(self) -> None:
        for label, values in (
            ("provider preference", self.provider_preference),
            ("selected overrides", self.selected_overrides),
            ("enabled features", self.enabled_features),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} IDs must be unique")


@dataclass(frozen=True)
class ProgramCoverage:
    """Conservative reachable program identities for one product profile."""

    roots: tuple[str, ...]
    reachable: frozenset[str]
    unresolved_edges: tuple[str, ...] = ()
    evidence_identity: str = ""
    edges: tuple["ProgramEdge", ...] = ()

    def coverage_for(self, product_profile: str) -> "ProgramCoverage":
        return self


class CoverageSource(Protocol):
    """Backend-neutral coverage interface implemented by IR and Atlas adapters."""

    def coverage_for(self, product_profile: str) -> ProgramCoverage: ...


@dataclass(frozen=True, order=True)
class ProgramEdge:
    """Known transfer used to expose or collapse implementation seams."""

    source: str
    target: str
    kind: str = "control-flow"
    evidence: str = ""


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
    recovery_level: RecoveryLevel | None = None
    evidence_grade: EvidenceGrade = EvidenceGrade.NONE
    contract: ImplementationContract | None = None
    execution_carrier: str = ""
    implementation_digest: str = ""
    region_id: str | None = None
    region_contract: ExecutionRegionContract | None = None

    def __post_init__(self) -> None:
        if not self.implementation_id:
            raise ValueError("implementation ID must not be empty")
        if self.origin is ImplementationOrigin.AUTHORED:
            if self.category is OverrideCategory.BASELINE:
                raise ValueError(
                    "authored implementations require faithful, enhancement, "
                    "behavioral, or instrumentation category")
        elif self.category is not OverrideCategory.BASELINE:
            raise ValueError(
                "interpreted and generated implementations use baseline category")
        if self.contract is not None and not self.contract.contract_id:
            raise ValueError("implementation contract ID must not be empty")
        if self.execution_carrier and self.region_id is None:
            raise ValueError(
                "only region/program providers declare an execution carrier; "
                "function implementations use carrier adapters"
            )
        if self.region_contract is not None:
            if self.region_id != self.region_contract.region_id:
                raise ValueError(
                    "implementation region ID must match its region contract"
                )
            if self.execution_carrier != self.region_contract.carrier_id:
                raise ValueError(
                    "implementation carrier must match its region contract"
                )
            attachment_targets = {
                item.target for item in self.region_contract.entries
            } | {
                item.continuation for item in self.region_contract.exits
            } | {self.region_contract.region_id}
            if not attachment_targets <= self.targets:
                raise ValueError(
                    "region implementations must target their region, entries, "
                    "and continuations"
                )
        if self.category is OverrideCategory.INSTRUMENTATION \
                and DependencyCapability.INSTRUMENTATION.value \
                not in self.required_capabilities:
            raise ValueError(
                "instrumentation implementations must declare the "
                "instrumentation capability"
            )


@dataclass(frozen=True)
class FeatureDescriptor:
    """Optional product behavior, presentation, or instrumentation policy."""

    feature_id: str
    category: FeatureCategory
    changes_authoritative_state: bool = False
    replay_channel: str = ""
    safe_boundaries: frozenset[str] = frozenset()
    default_value: object = False
    required_capabilities: frozenset[str] = frozenset()
    required_services: frozenset[str] = frozenset()
    required_assets: frozenset[str] = frozenset()
    supported_platforms: frozenset[str] = frozenset()
    feature_digest: str = ""

    def __post_init__(self) -> None:
        if not self.feature_id:
            raise ValueError("feature ID must not be empty")
        if self.category is FeatureCategory.PRESENTATION \
                and self.changes_authoritative_state:
            raise ValueError(
                "presentation features must not mutate authoritative state"
            )
        if self.category is FeatureCategory.BEHAVIORAL:
            if not self.changes_authoritative_state:
                raise ValueError(
                    "behavioral features must declare authoritative-state change"
                )
            if not self.replay_channel:
                raise ValueError(
                    "behavioral features require a replay event channel"
                )
            if not self.safe_boundaries:
                raise ValueError(
                    "behavioral features require at least one safe boundary"
                )
        if self.category is FeatureCategory.INSTRUMENTATION \
                and DependencyCapability.INSTRUMENTATION.value \
                not in self.required_capabilities:
            raise ValueError(
                "instrumentation features must declare the instrumentation "
                "capability"
            )
        try:
            json.dumps(self.default_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("feature default value must be JSON-serializable") from exc


@dataclass(frozen=True)
class FeatureCatalog:
    features: tuple[FeatureDescriptor, ...] = ()

    def __post_init__(self) -> None:
        identities = [item.feature_id for item in self.features]
        if len(set(identities)) != len(identities):
            raise ValueError("feature IDs must be unique")


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
class BackendAdapter:
    """A backend-specific bridge for one backend-neutral implementation.

    The implementation catalog owns the semantic body and stable target
    identity.  A bridge is allowed to marshal that body through a particular
    carrier (CPU registers, a recovered ABI call, or a native state object),
    but it must not become a second implementation authority.
    """

    adapter_id: str
    carrier_id: str
    activate: Callable[[object, tuple[str, ...]], None]
    adapter_digest: str = ""

    def __post_init__(self) -> None:
        if not self.adapter_id:
            raise ValueError("adapter ID must not be empty")
        if not self.carrier_id:
            raise ValueError("adapter carrier ID must not be empty")
        if not self.adapter_digest:
            raise ValueError("adapter digest must not be empty")


@dataclass(frozen=True)
class RegionAdapter:
    """Bridge from a surrounding carrier into one long-lived region carrier."""

    adapter_id: str
    host_carrier_id: str
    region_carrier_id: str
    activate: Callable[[object, "ResolvedExecutionRegion"], None]
    adapter_digest: str = ""

    def __post_init__(self) -> None:
        if not self.adapter_id:
            raise ValueError("region adapter ID must not be empty")
        if not self.host_carrier_id or not self.region_carrier_id:
            raise ValueError("region adapter carriers must not be empty")
        if not self.adapter_digest:
            raise ValueError("region adapter digest must not be empty")


# Carrier IDs describe calling/state mechanics at the activation seam, not a
# whole-game recovery level.
INTERPRETED_CPU_CARRIER = "interpreted-cpu"
GENERATED_VMLESS_CARRIER = "generated-vmless-cpu"
GENERATED_CPULESS_CARRIER = "generated-cpuless"
DOS_MEMORY_CARRIER = "dos-memory"
NATIVE_STATE_CARRIER = "native-state"


@dataclass(frozen=True)
class ImplementationEntry:
    descriptor: ImplementationDescriptor
    implementation: Callable | None = None
    adapters: tuple[BackendAdapter, ...] = ()
    region_adapters: tuple[RegionAdapter, ...] = ()

    def __post_init__(self) -> None:
        carrier_ids = [adapter.carrier_id for adapter in self.adapters]
        adapter_ids = [adapter.adapter_id for adapter in self.adapters]
        if len(set(carrier_ids)) != len(carrier_ids):
            raise ValueError(
                f"implementation {self.descriptor.implementation_id!r} has "
                "more than one adapter for the same carrier")
        if len(set(adapter_ids)) != len(adapter_ids):
            raise ValueError("adapter IDs must be unique per implementation")
        region_hosts = [adapter.host_carrier_id for adapter in self.region_adapters]
        region_adapter_ids = [adapter.adapter_id for adapter in self.region_adapters]
        if len(set(region_hosts)) != len(region_hosts):
            raise ValueError(
                "implementation has more than one region adapter for a host carrier"
            )
        if len(set(region_adapter_ids)) != len(region_adapter_ids):
            raise ValueError("region adapter IDs must be unique per implementation")
        contract = self.descriptor.region_contract
        if self.region_adapters and contract is None:
            raise ValueError("region adapters require an execution region contract")
        if contract is not None and any(
            adapter.region_carrier_id != contract.carrier_id
            for adapter in self.region_adapters
        ):
            raise ValueError(
                "region adapter carrier must match the execution region contract"
            )


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

    def entry(self, implementation_id: str) -> ImplementationEntry:
        for entry in self.entries:
            if entry.descriptor.implementation_id == implementation_id:
                return entry
        raise KeyError(implementation_id)


def bind_plan_implementations(
    runtime: object,
    plan: "ExecutionPlan",
    *,
    carrier_id: str,
) -> None:
    """Install plan-selected backend bridges into an already-created runtime.

    Selection remains entirely in :class:`ExecutionPlan`; this function only
    binds the adapter that can cross the selected runtime's carrier.  Whole
    program providers own their own launch mechanics and consequently need no
    per-target adapter here.  A selected function implementation without a
    bridge for ``carrier_id`` is a configuration error, never a silent fallback.
    """
    previous_plan = getattr(runtime, "execution_plan", None)
    previous_carrier = getattr(runtime, "execution_carrier_id", None)
    if previous_plan is not None and previous_plan.plan_digest != plan.plan_digest:
        raise RuntimeError(
            "runtime is already bound to a different execution plan: "
            f"{previous_plan.plan_digest[:12]} != {plan.plan_digest[:12]}"
        )
    if previous_carrier is not None and previous_carrier != carrier_id:
        raise RuntimeError(
            "runtime is already bound to a different execution carrier: "
            f"{previous_carrier!r} != {carrier_id!r}"
        )
    runtime.execution_plan = plan
    runtime.execution_carrier_id = carrier_id

    for region in plan.regions:
        entry = plan.catalog.entry(region.implementation_id)
        adapter = next((
            item for item in entry.region_adapters
            if item.host_carrier_id == carrier_id
            and item.adapter_id == region.adapter_id
        ), None)
        if adapter is None:
            raise RuntimeError(
                f"selected region {region.region_id!r} has no planned adapter "
                f"for carrier {carrier_id!r}"
            )
        adapter.activate(runtime, region)

    suppressed_bindings = {
        (binding.target, binding.implementation_id)
        for region in plan.regions
        for binding in region.suppressed_bindings
    }
    targets_by_implementation: dict[str, list[str]] = {}
    for binding in plan.bindings:
        if (binding.target, binding.implementation_id) in suppressed_bindings:
            continue
        targets_by_implementation.setdefault(binding.implementation_id, []).append(
            binding.target
        )
    for descriptor in plan.implementations:
        if descriptor.category in {
            OverrideCategory.ENHANCEMENT,
            OverrideCategory.INSTRUMENTATION,
        }:
            targets_by_implementation.setdefault(
                descriptor.implementation_id, []
            ).extend(descriptor.targets)

    entries = {
        item.descriptor.implementation_id: item for item in plan.catalog.entries
    }
    descriptors = {
        item.implementation_id: item for item in plan.implementations
    }
    for implementation_id, targets in sorted(targets_by_implementation.items()):
        entry = entries[implementation_id]
        adapter = next(
            (item for item in entry.adapters if item.carrier_id == carrier_id),
            None,
        )
        if adapter is not None:
            adapter.activate(runtime, tuple(sorted(targets)))
            continue

        descriptor = descriptors[implementation_id]
        if descriptor.region_id is not None or (
            descriptor.origin is ImplementationOrigin.INTERPRETED
        ):
            # A region provider is launched by its owning backend.  The
            # interpreted baseline uses the untouched bytes and has no bridge.
            continue
        if entry.adapters:
            available = ", ".join(sorted(item.carrier_id for item in entry.adapters))
            raise RuntimeError(
                f"selected implementation {implementation_id!r} has no "
                f"adapter for carrier {carrier_id!r} (available: {available})"
            )
        raise RuntimeError(
            f"selected implementation {implementation_id!r} has no backend "
            "adapter"
        )


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
class ResolvedExecutionRegion:
    """One selected island and the exact handoff bridge chosen by the planner."""

    region_id: str
    implementation_id: str
    host_carrier_id: str
    region_carrier_id: str
    adapter_id: str
    adapter_digest: str
    state_ownership: RegionStateOwnership
    entries: tuple[RegionEntryPoint, ...]
    exits: tuple[RegionExitPoint, ...]
    covered_targets: tuple[str, ...]
    suppressed_bindings: tuple[PlanBinding, ...]
    replay_boundaries: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDecision:
    implementation_id: str
    selected: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetResolution:
    target: str
    candidates: tuple[CandidateDecision, ...]


@dataclass(frozen=True)
class ExecutionBoundary:
    """A known program edge crossing selected implementation ownership."""

    source: str
    target: str
    kind: str
    source_implementation_id: str
    target_implementation_id: str
    carrier_id: str
    adapter_id: str = ""


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
    execution_carrier: str
    target_resolutions: tuple[TargetResolution, ...]
    active_boundaries: tuple[ExecutionBoundary, ...]
    collapsed_edge_count: int
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
    enabled_features: tuple[str, ...]
    selected_regions: tuple[ResolvedExecutionRegion, ...]
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
            instructions = sorted({
                item.generation_instruction
                for item in self.bootstrap_artifacts
                if not item.materializable and item.generation_instruction
            })
            if instructions:
                lines.append(
                    "bootstrap generation: " + " | ".join(instructions)
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
    features: tuple[FeatureDescriptor, ...]
    regions: tuple[ResolvedExecutionRegion, ...]
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
    binding_counts: dict[str, int] = {}
    for binding in report.bindings:
        binding_counts[binding.implementation_id] = (
            binding_counts.get(binding.implementation_id, 0) + 1
        )
    lines = [
        f"execution profile: {plan.configuration.profile}",
        f"program: {plan.configuration.program_identity}",
        f"plan digest: {plan.plan_digest}",
        f"bootstrap provider: {report.bootstrap_provider_id} ({report.bootstrap_kind})",
        f"execution carrier: {report.execution_carrier or 'unspecified'}",
        f"reachable identities: {len(report.reachable)}",
        f"bound identities: {len(report.bindings)}",
        f"active implementation boundaries: {len(report.active_boundaries)}",
        f"collapsed known edges: {report.collapsed_edge_count}",
        "required capabilities: "
        + (", ".join(report.required_capabilities) or "none"),
        f"package ready: {str(report.package_ready).lower()}",
    ]
    if report.enabled_features:
        lines.append("features: " + ", ".join(report.enabled_features))
    lines.extend(
        f"execution region: {item.region_id} via {item.adapter_id} "
        f"({len(item.covered_targets)} contextual targets, "
        f"{len(item.suppressed_bindings)} dormant inner bindings)"
        for item in report.selected_regions
    )
    lines.extend(
        f"implementation: {implementation_id} ({count} identities)"
        for implementation_id, count in sorted(binding_counts.items())
    )
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
    enabled_features: Iterable[str] = (),
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
        enabled_features=tuple(enabled_features),
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


def _candidate_rejections(
    implementation: ImplementationDescriptor,
    entry: ImplementationEntry,
    policy: ExecutionPolicy,
    build_target: BuildTarget | None,
    execution_carrier: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    blocked = sorted(
        implementation.required_capabilities & policy.forbidden_capabilities
    )
    if blocked:
        reasons.append("forbidden capabilities: " + ", ".join(blocked))
    if (
        build_target is not None
        and implementation.supported_platforms
        and build_target.platform not in implementation.supported_platforms
    ):
        reasons.append(f"unsupported platform: {build_target.platform}")
    if (
        implementation.origin is ImplementationOrigin.AUTHORED
        and implementation.evidence_grade < policy.minimum_authored_evidence
    ):
        reasons.append(
            "insufficient evidence: "
            f"{implementation.evidence_grade.name.lower()} < "
            f"{policy.minimum_authored_evidence.name.lower()}"
        )
    if execution_carrier:
        contract = implementation.region_contract
        if contract is not None:
            adapted = any(
                adapter.host_carrier_id == execution_carrier
                and adapter.region_carrier_id == contract.carrier_id
                for adapter in entry.region_adapters
            )
            if not adapted:
                reasons.append(
                    f"no region adapter from carrier: {execution_carrier}"
                )
            return tuple(reasons)
        directly_owned = implementation.execution_carrier == execution_carrier
        adapted = any(
            adapter.carrier_id == execution_carrier for adapter in entry.adapters
        )
        # Descriptors without an intrinsic carrier are function/point bodies;
        # they are executable only through an explicit adapter.
        if not directly_owned and not adapted:
            reasons.append(f"no adapter for carrier: {execution_carrier}")
    return tuple(reasons)


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


def _execution_carrier(
    coverage: ProgramCoverage,
    implementations: tuple[ImplementationDescriptor, ...],
    catalog: ImplementationCatalog,
    preference: tuple[str, ...],
    policy: ExecutionPolicy,
    build_target: BuildTarget | None,
) -> str:
    """Resolve the carrier from the selected provider at every program root."""
    carriers: set[str] = set()
    for root in coverage.roots:
        candidates = [
            item for item in _ordered_candidates(root, implementations, preference)
            if item.execution_carrier
        ]
        selected = next((
            item for item in candidates
            if not _candidate_rejections(
                item, catalog.entry(item.implementation_id), policy,
                build_target, "",
            )
        ), None)
        if selected is not None:
            carriers.add(selected.execution_carrier)
    if len(carriers) > 1:
        raise ValueError(
            "program roots select incompatible execution carriers: "
            + ", ".join(sorted(carriers))
        )
    return next(iter(carriers), "")


def _plan_digest(
    configuration: ExecutionConfiguration,
    coverage: ProgramCoverage,
    bindings: tuple[PlanBinding, ...],
    selected: tuple[ImplementationDescriptor, ...],
    services: tuple[RuntimeServiceDescriptor, ...],
    features: tuple[FeatureDescriptor, ...],
    execution_carrier: str,
    regions: tuple[ResolvedExecutionRegion, ...],
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
            "minimum_authored_evidence": int(
                configuration.execution_policy.minimum_authored_evidence
            ),
        },
        "execution_carrier": execution_carrier,
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
        "enabled_features": list(configuration.enabled_features),
        "build": (
            None if configuration.build_target is None else
            [configuration.build_target.platform, configuration.build_target.package_format]
        ),
        "coverage": {
            "identity": coverage.evidence_identity,
            "roots": list(coverage.roots),
            "reachable": sorted(coverage.reachable),
            "unresolved_edges": list(coverage.unresolved_edges),
            "edges": [
                [item.source, item.target, item.kind, item.evidence]
                for item in sorted(coverage.edges)
            ],
        },
        "bindings": [[item.target, item.implementation_id] for item in bindings],
        "regions": [
            {
                "id": item.region_id,
                "implementation": item.implementation_id,
                "host_carrier": item.host_carrier_id,
                "region_carrier": item.region_carrier_id,
                "adapter": item.adapter_id,
                "adapter_digest": item.adapter_digest,
                "state_ownership": item.state_ownership.value,
                "covered_targets": list(item.covered_targets),
                "suppressed_bindings": [
                    [binding.target, binding.implementation_id]
                    for binding in item.suppressed_bindings
                ],
            }
            for item in regions
        ],
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
                "recovery_level": (
                    None if item.recovery_level is None
                    else item.recovery_level.value
                ),
                "evidence_grade": int(item.evidence_grade),
                "contract": (
                    None if item.contract is None else item.contract.contract_id
                ),
                "execution_carrier": item.execution_carrier,
                "region": item.region_id,
                "region_contract": (
                    None if item.region_contract is None else {
                        "carrier": item.region_contract.carrier_id,
                        "state_ownership": item.region_contract.state_ownership.value,
                        "entries": [
                            [entry.entry_id, entry.target]
                            for entry in item.region_contract.entries
                        ],
                        "exits": [
                            [exit.exit_id, exit.continuation]
                            for exit in item.region_contract.exits
                        ],
                        "covered_targets": sorted(
                            item.region_contract.covered_targets
                        ),
                        "replay_boundaries": sorted(
                            item.region_contract.replay_boundaries
                        ),
                    }
                ),
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
        "features": [
            {
                "id": item.feature_id,
                "digest": item.feature_digest,
                "category": item.category.value,
                "authoritative": item.changes_authoritative_state,
                "replay_channel": item.replay_channel,
                "safe_boundaries": sorted(item.safe_boundaries),
            }
            for item in features
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_composition_digest(plan: ExecutionPlan) -> str:
    """Identify selected executable code and runtime services, not plan evidence.

    Replay execution profiles feed dynamic observations back into coverage
    sources such as the Execution Atlas. Hashing ``plan_digest`` into a replay
    profile would therefore create a cycle: replay evidence changes coverage,
    coverage changes the plan, and the unchanged runtime acquires a new replay
    identity. This digest deliberately excludes coverage, policy, verification,
    packaging, and bootstrap metadata while retaining every selected
    implementation binding and executable service digest.
    """
    implementations = {
        item.implementation_id: item for item in plan.implementations
    }
    payload = {
        "execution_carrier": plan.report.execution_carrier,
        "bindings": sorted(
            (item.target, item.implementation_id) for item in plan.bindings
        ),
        "implementations": [
            {
                "id": item.implementation_id,
                "digest": item.implementation_digest,
                "origin": item.origin.value,
                "category": item.category.value,
                "recovery_level": (
                    None if item.recovery_level is None
                    else item.recovery_level.value
                ),
                "contract": (
                    None if item.contract is None else item.contract.contract_id
                ),
                "region": item.region_id,
                "region_contract": (
                    None if item.region_contract is None else {
                        "carrier": item.region_contract.carrier_id,
                        "state_ownership": item.region_contract.state_ownership.value,
                        "entries": [
                            [entry.entry_id, entry.target]
                            for entry in item.region_contract.entries
                        ],
                        "exits": [
                            [exit.exit_id, exit.continuation]
                            for exit in item.region_contract.exits
                        ],
                        "covered_targets": sorted(
                            item.region_contract.covered_targets
                        ),
                    }
                ),
            }
            for item in sorted(
                implementations.values(),
                key=lambda value: value.implementation_id,
            )
        ],
        "regions": [
            {
                "id": item.region_id,
                "implementation": item.implementation_id,
                "host_carrier": item.host_carrier_id,
                "region_carrier": item.region_carrier_id,
                "adapter": item.adapter_id,
                "adapter_digest": item.adapter_digest,
                "state_ownership": item.state_ownership.value,
                "covered_targets": list(item.covered_targets),
            }
            for item in plan.regions
        ],
        "services": [
            {
                "id": item.service_id,
                "digest": item.implementation_digest,
            }
            for item in sorted(plan.services, key=lambda value: value.service_id)
        ],
        "features": [
            {
                "id": item.feature_id,
                "digest": item.feature_digest,
                "category": item.category.value,
            }
            for item in sorted(plan.features, key=lambda value: value.feature_id)
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
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
    missing_provider_digests = [
        item.provider_id for item in components if not item.provider_digest
    ]
    if missing_provider_digests:
        raise ValueError(
            "selected bootstrap providers require stable content digests: "
            + ", ".join(missing_provider_digests)
        )
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
    feature_catalog: FeatureCatalog = FeatureCatalog(),
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
    feature_by_id = {
        item.feature_id: item for item in feature_catalog.features
    }
    unknown_features = sorted(
        set(configuration.enabled_features) - set(feature_by_id)
    )
    if unknown_features:
        raise ValueError("unknown enabled features: " + ", ".join(unknown_features))
    selected_features = tuple(
        feature_by_id[feature_id]
        for feature_id in configuration.enabled_features
    )
    missing_feature_digests = tuple(
        item.feature_id for item in selected_features if not item.feature_digest
    )
    if missing_feature_digests:
        raise ValueError(
            "selected features require stable content digests: "
            + ", ".join(missing_feature_digests)
        )
    selected_authored = tuple(
        item for item in implementation_items
        if item.implementation_id in selected_override_set
        and item.origin is ImplementationOrigin.AUTHORED
    )
    for override in selected_authored:
        if not override.targets:
            raise ValueError(
                f"selected override {override.implementation_id!r} requires "
                "an attachment target"
            )
        missing_targets = override.targets - coverage.reachable
        if missing_targets:
            raise ValueError(
                f"selected override {override.implementation_id!r} targets "
                "outside conservative coverage: "
                + ", ".join(sorted(missing_targets))
            )
    selected_attachments = tuple(
        item for item in selected_authored
        if item.category in {
            OverrideCategory.ENHANCEMENT,
            OverrideCategory.INSTRUMENTATION,
        }
    )
    authoritative_items = tuple(
        item for item in implementation_items
        if item.category not in {
            OverrideCategory.ENHANCEMENT,
            OverrideCategory.INSTRUMENTATION,
        }
    )
    for target in sorted(coverage.reachable):
        owners = [
            item.implementation_id for item in authoritative_items
            if target in item.targets and item.implementation_id in selected_override_set
        ]
        if len(owners) > 1:
            raise ValueError(
                f"multiple selected authored implementations own {target}: "
                + ", ".join(sorted(owners))
            )
    service_items = service_catalog.services
    service_by_id = {service.service_id: service for service in service_items}

    selected_first = tuple(configuration.selected_overrides) + tuple(
        item for item in configuration.provider_preference
        if item not in configuration.selected_overrides
    )
    execution_carrier = _execution_carrier(
        coverage,
        authoritative_items,
        implementation_catalog,
        selected_first,
        configuration.execution_policy,
        configuration.build_target,
    )
    for attachment in selected_attachments:
        reasons = _candidate_rejections(
            attachment,
            implementation_catalog.entry(attachment.implementation_id),
            configuration.execution_policy,
            configuration.build_target,
            execution_carrier,
        )
        if reasons:
            raise ValueError(
                f"selected attachment {attachment.implementation_id!r} is "
                "incompatible: " + "; ".join(reasons)
            )
    bindings: list[PlanBinding] = []
    target_resolutions: list[TargetResolution] = []
    selected_by_id: dict[str, ImplementationDescriptor] = {
        item.implementation_id: item for item in selected_attachments
    }
    unresolved: list[str] = []
    blocked_capabilities: dict[str, set[str]] = {}
    packaging_incompatible: list[str] = []
    for target in sorted(coverage.reachable):
        candidates = _ordered_candidates(
            target, authoritative_items, selected_first
        )
        rejection_by_id = {
            candidate.implementation_id: _candidate_rejections(
                candidate,
                implementation_catalog.entry(candidate.implementation_id),
                configuration.execution_policy,
                configuration.build_target,
                execution_carrier,
            )
            for candidate in candidates
        }
        selected = next((
            candidate for candidate in candidates
            if not rejection_by_id[candidate.implementation_id]
        ), None)
        target_resolutions.append(TargetResolution(
            target,
            tuple(CandidateDecision(
                candidate.implementation_id,
                selected is not None
                and candidate.implementation_id == selected.implementation_id,
                rejection_by_id[candidate.implementation_id] or (
                    ()
                    if selected is None
                    or candidate.implementation_id == selected.implementation_id
                    else (
                        "lower preference than selected: "
                        + selected.implementation_id,
                    )
                ),
            ) for candidate in candidates),
        ))
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
    binding_items = tuple(bindings)
    selected_region_descriptors = tuple(
        item for item in selected if item.region_contract is not None
    )
    covered_by_region: dict[str, str] = {}
    resolved_regions: list[ResolvedExecutionRegion] = []
    for descriptor in selected_region_descriptors:
        contract = descriptor.region_contract
        assert contract is not None
        selected_targets = {
            binding.target for binding in binding_items
            if binding.implementation_id == descriptor.implementation_id
        }
        if not descriptor.targets <= selected_targets:
            raise ValueError(
                f"execution region {contract.region_id!r} was selected only "
                "partially; region entry, exit, and identity targets must resolve "
                "as one unit"
            )
        missing_covered = contract.covered_targets - coverage.reachable
        if missing_covered:
            raise ValueError(
                f"execution region {contract.region_id!r} covers targets "
                "outside conservative coverage: "
                + ", ".join(sorted(missing_covered))
            )
        for target in contract.covered_targets:
            previous = covered_by_region.setdefault(target, contract.region_id)
            if previous != contract.region_id:
                raise ValueError(
                    f"selected execution regions {previous!r} and "
                    f"{contract.region_id!r} overlap at {target!r}; nested "
                    "region ownership must be declared explicitly"
                )
        entry = implementation_catalog.entry(descriptor.implementation_id)
        adapter = next((
            item for item in entry.region_adapters
            if item.host_carrier_id == execution_carrier
            and item.region_carrier_id == contract.carrier_id
        ), None)
        if adapter is None:
            raise ValueError(
                f"execution region {contract.region_id!r} has no adapter from "
                f"carrier {execution_carrier!r}"
            )
        suppressed = tuple(
            binding for binding in binding_items
            if binding.target in contract.covered_targets
            and binding.implementation_id != descriptor.implementation_id
        )
        resolved_regions.append(ResolvedExecutionRegion(
            region_id=contract.region_id,
            implementation_id=descriptor.implementation_id,
            host_carrier_id=execution_carrier,
            region_carrier_id=contract.carrier_id,
            adapter_id=adapter.adapter_id,
            adapter_digest=adapter.adapter_digest,
            state_ownership=contract.state_ownership,
            entries=contract.entries,
            exits=contract.exits,
            covered_targets=tuple(sorted(contract.covered_targets)),
            suppressed_bindings=suppressed,
            replay_boundaries=tuple(sorted(contract.replay_boundaries)),
        ))
    region_items = tuple(sorted(
        resolved_regions, key=lambda item: item.region_id
    ))
    missing_implementation_digests = tuple(
        item.implementation_id for item in selected
        if not item.implementation_digest
    )
    if missing_implementation_digests:
        raise ValueError(
            "selected implementations require stable content digests: "
            + ", ".join(missing_implementation_digests)
        )
    if (
        configuration.verification_policy.mode == "differential"
        and any(
            item.category is OverrideCategory.BEHAVIORAL for item in selected
        )
    ):
        behavioral = ", ".join(
            item.implementation_id for item in selected
            if item.category is OverrideCategory.BEHAVIORAL
        )
        raise ValueError(
            "faithful differential verification cannot select behavioral "
            f"modifications: {behavioral}"
        )
    behavioral_features = tuple(
        item for item in selected_features
        if item.category is FeatureCategory.BEHAVIORAL
    )
    if (
        configuration.verification_policy.mode == "differential"
        and behavioral_features
    ):
        raise ValueError(
            "faithful differential verification cannot enable behavioral "
            "features: "
            + ", ".join(item.feature_id for item in behavioral_features)
        )
    required_service_set = (
        set(configuration.product_services)
        | set(bootstrap_services)
        | {
        service_id for item in selected for service_id in item.required_services
        }
        | {
            service_id for item in selected_features
            for service_id in item.required_services
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
    missing_service_digests = tuple(
        service.service_id
        for service in selected_services
        if not service.implementation_digest
    )
    if missing_service_digests:
        raise ValueError(
            "selected runtime services require stable content digests: "
            + ", ".join(missing_service_digests)
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
        | {
            asset
            for item in selected_features
            for asset in item.required_assets
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
        set(packaging_incompatible) | set(incompatible_services) | {
            f"feature:{item.feature_id}"
            for item in selected_features
            if configuration.build_target is not None
            and item.supported_platforms
            and configuration.build_target.platform not in item.supported_platforms
        }
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
    for item in selected_features:
        for capability in item.required_capabilities:
            capability_consumers.setdefault(capability, set()).add(
                f"feature:{item.feature_id}"
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
        | {
            capability
            for item in selected_features
            for capability in item.required_capabilities
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
    binding_by_target = {
        item.target: item.implementation_id for item in bindings
    }
    active_boundaries: list[ExecutionBoundary] = []
    collapsed_edge_count = 0
    for edge in sorted(set(coverage.edges)):
        source_implementation = binding_by_target.get(edge.source)
        target_implementation = binding_by_target.get(edge.target)
        if source_implementation is None or target_implementation is None:
            continue
        if source_implementation == target_implementation:
            collapsed_edge_count += 1
            continue
        target_entry = implementation_catalog.entry(target_implementation)
        target_adapter = next((
            item for item in target_entry.adapters
            if item.carrier_id == execution_carrier
        ), None)
        active_boundaries.append(ExecutionBoundary(
            source=edge.source,
            target=edge.target,
            kind=edge.kind,
            source_implementation_id=source_implementation,
            target_implementation_id=target_implementation,
            carrier_id=execution_carrier,
            adapter_id="" if target_adapter is None else target_adapter.adapter_id,
        ))
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
    report = DetachmentReport(
        reachable=tuple(sorted(coverage.reachable)),
        bindings=binding_items,
        execution_carrier=execution_carrier,
        target_resolutions=tuple(target_resolutions),
        active_boundaries=tuple(active_boundaries),
        collapsed_edge_count=collapsed_edge_count,
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
        enabled_features=tuple(item.feature_id for item in selected_features),
        selected_regions=region_items,
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
        configuration, coverage, binding_items, selected, selected_services,
        selected_features, execution_carrier, region_items,
    )
    return ExecutionPlan(
        configuration=configuration,
        bootstrap_provider=configuration.bootstrap_provider,
        coverage_identity=coverage.evidence_identity,
        bindings=binding_items,
        implementations=selected,
        features=selected_features,
        regions=region_items,
        catalog=implementation_catalog,
        services=selected_services,
        report=report,
        plan_digest=digest,
    )
