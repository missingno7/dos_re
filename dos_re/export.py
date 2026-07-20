"""Closed-world release export for a resolved dos_re execution plan."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .execution import (
    BootstrapArtifact,
    BootstrapExportMode,
    DependencyCapability,
    ExecutionPlan,
)


IMPORT_CAPABILITIES = {
    "dos_re.bootstrap_runtime": DependencyCapability.DOS_RE_RUNTIME.value,
    "dos_re.cpu": DependencyCapability.CPU_MODEL.value,
    "dos_re.cpu386": DependencyCapability.CPU_MODEL.value,
    "dos_re.execution": DependencyCapability.DEVELOPMENT_TOOLING.value,
    "dos_re.hooks": DependencyCapability.DEVELOPMENT_TOOLING.value,
    "dos_re.player": DependencyCapability.DEVELOPMENT_TOOLING.value,
    "dos_re.pm_player": DependencyCapability.DEVELOPMENT_TOOLING.value,
    "dos_re.replay": DependencyCapability.REPLAY.value,
    "dos_re.runtime": DependencyCapability.DOS_RE_RUNTIME.value,
    "dos_re.snapshot": DependencyCapability.SNAPSHOTS.value,
    "dos_re.snapshot_headless": DependencyCapability.SNAPSHOTS.value,
    "dos_re.snapshot_runtime": DependencyCapability.DOS_RE_RUNTIME.value,
    "dos_re.verification": DependencyCapability.ORACLE.value,
    "dos_re.pm_verification": DependencyCapability.ORACLE.value,
}
DYNAMIC_RUNTIME_MODULES = ("ctypes", "importlib", "pkgutil", "runpy")


@dataclass(frozen=True)
class ExportFile:
    source: Path
    destination: str
    required_capabilities: frozenset[str] = frozenset()
    asset_id: str | None = None


@dataclass(frozen=True)
class ExportManifest:
    plan_digest: str
    target: str
    launcher: str
    bootstrap_provider_id: str
    bootstrap_artifacts: tuple[tuple[str, str], ...]
    required_capabilities: tuple[str, ...]
    files: tuple[tuple[str, str], ...]


class ExportError(RuntimeError):
    pass


RELEASE_MANIFEST_SCHEMA = "dos_re_release/v2"


def _runtime_nodes(tree: ast.AST):
    """Yield AST nodes that can execute at runtime.

    Annotation-only imports inside ``if TYPE_CHECKING`` are development-time
    type information, not packaged capabilities.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            or isinstance(node.test, ast.Attribute)
            and node.test.attr == "TYPE_CHECKING"
        ):
            for child in node.orelse:
                yield child
                yield from _runtime_nodes(child)
            continue
        yield node
        yield from _runtime_nodes(node)


def _import_names(path: Path, destination: Path) -> set[str]:
    if path.suffix != ".py":
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = destination.with_suffix("").parts
    package_parts = module_parts[:-1]
    names: set[str] = set()
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - node.level + 1)
                base_parts = package_parts[:keep]
                if node.module:
                    base_parts += tuple(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            if base:
                names.add(base)
            names.update(
                ".".join(part for part in (base, alias.name) if part)
                for alias in node.names
                if alias.name != "*"
            )
    return names


def _dynamic_loading_calls(path: Path) -> tuple[str, ...]:
    if path.suffix != ".py":
        return ()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in _runtime_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {
            "__import__", "eval", "exec",
        }:
            found.add(node.func.id)
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
        ):
            found.add("importlib.import_module")
    return tuple(sorted(found))


def _import_capability(name: str) -> tuple[str, str] | None:
    for module, capability in IMPORT_CAPABILITIES.items():
        if name == module or name.startswith(module + "."):
            return module, capability
    return None


def _validate_files(
    plan: ExecutionPlan,
    files: tuple[ExportFile, ...],
    launcher: str,
    *,
    generated_destinations: tuple[str, ...] = (),
) -> None:
    destinations: set[str] = set()
    packaged_assets: set[str] = set()
    required = set(plan.report.required_capabilities)
    for item in files:
        source = Path(item.source)
        destination = Path(item.destination)
        if not source.is_file():
            raise ExportError(f"export source does not exist: {source}")
        if destination.is_absolute() or ".." in destination.parts:
            raise ExportError(f"export destination escapes artifact: {item.destination}")
        normalized = destination.as_posix()
        if normalized in destinations:
            raise ExportError(f"duplicate export destination: {normalized}")
        destinations.add(normalized)
        if item.asset_id is not None:
            if item.asset_id in packaged_assets:
                raise ExportError(f"duplicate packaged asset: {item.asset_id}")
            packaged_assets.add(item.asset_id)
        detached_requirements = item.required_capabilities - required
        if detached_requirements:
            raise ExportError(
                f"{source} is tagged with capabilities outside the selected "
                "release closure: " + ", ".join(sorted(detached_requirements))
            )
        for imported in sorted(_import_names(source, destination)):
            if any(
                imported == module or imported.startswith(module + ".")
                for module in DYNAMIC_RUNTIME_MODULES
            ):
                raise ExportError(
                    f"{source} imports release-forbidden dynamic runtime "
                    f"module {imported!r}"
                )
            dependency = _import_capability(imported)
            if dependency is not None and dependency[1] not in required:
                module, capability = dependency
                raise ExportError(
                    f"{source} imports {module!r} through {imported!r}, but "
                    f"capability {capability!r} is outside the selected "
                    "release closure"
                )
        dynamic_calls = _dynamic_loading_calls(source)
        if dynamic_calls:
            raise ExportError(
                f"{source} uses release-forbidden dynamic loading/evaluation: "
                + ", ".join(dynamic_calls)
            )
    for generated in generated_destinations:
        destination = Path(generated)
        if destination.is_absolute() or ".." in destination.parts:
            raise ExportError(f"bootstrap destination escapes artifact: {generated}")
        normalized = destination.as_posix()
        if normalized in destinations:
            raise ExportError(f"duplicate export destination: {normalized}")
        destinations.add(normalized)
    if Path(launcher).as_posix() not in destinations:
        raise ExportError(f"launcher {launcher!r} is not in the export closure")
    missing_assets = set(plan.report.required_assets) - packaged_assets
    undeclared_assets = packaged_assets - set(plan.report.required_assets)
    if missing_assets or undeclared_assets:
        raise ExportError(
            "packaged asset closure differs from execution plan; missing="
            + repr(sorted(missing_assets))
            + ", undeclared="
            + repr(sorted(undeclared_assets))
        )


def _bootstrap_artifacts(plan: ExecutionPlan) -> tuple[BootstrapArtifact, ...]:
    return tuple(
        artifact
        for provider in plan.bootstrap_provider.components()
        for artifact in provider.artifacts
    )


def export_release(
    plan: ExecutionPlan,
    files: tuple[ExportFile, ...],
    destination: str | Path,
    *,
    launcher: str,
) -> ExportManifest:
    """Export and audit exactly the supplied product closure.

    The caller must enumerate files; directory copying and dynamic discovery
    are intentionally unsupported. The finished artifact is hashed and bound
    to the immutable release plan.
    """
    if plan.configuration.profile != "release":
        raise ExportError("only a release-profile plan may be exported")
    if not plan.report.package_ready:
        raise ExportError("release plan is not package-ready")
    bootstrap_artifacts = _bootstrap_artifacts(plan)
    included_bootstrap = tuple(
        ExportFile(Path(item.source_path), item.runtime_path)
        for item in bootstrap_artifacts
        if item.export_mode is BootstrapExportMode.INCLUDE
    )
    generated_bootstrap = tuple(
        item for item in bootstrap_artifacts
        if item.export_mode is BootstrapExportMode.GENERATE
    )
    files = files + included_bootstrap
    _validate_files(
        plan,
        files,
        launcher,
        generated_destinations=tuple(
            item.runtime_path for item in generated_bootstrap
        ),
    )

    destination = Path(destination)
    if destination.exists():
        raise ExportError(f"refusing to overwrite existing artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.pending-", dir=destination.parent
    ))
    try:
        hashes: list[tuple[str, str]] = []
        for item in sorted(files, key=lambda candidate: candidate.destination):
            target = staging / item.destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item.source, target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            hashes.append((Path(item.destination).as_posix(), digest))
        for artifact in sorted(
            generated_bootstrap,
            key=lambda item: item.runtime_path,
        ):
            if artifact.materializer is None:
                raise ExportError(
                    f"bootstrap artifact {artifact.artifact_id!r} has no materializer"
                )
            target = staging / artifact.runtime_path
            target.parent.mkdir(parents=True, exist_ok=True)
            artifact.materializer(target)
            if not target.is_file():
                raise ExportError(
                    f"bootstrap materializer did not create "
                    f"{artifact.runtime_path!r}"
                )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if artifact.expected_sha256 and digest != artifact.expected_sha256:
                raise ExportError(
                    f"generated bootstrap artifact {artifact.artifact_id!r} "
                    "does not match its declared hash"
                )
            hashes.append((Path(artifact.runtime_path).as_posix(), digest))
        expected_bootstrap_hashes = {
            Path(item.runtime_path).as_posix(): item.expected_sha256
            for item in bootstrap_artifacts
            if item.expected_sha256
        }
        for relative, digest in hashes:
            if (
                relative in expected_bootstrap_hashes
                and digest != expected_bootstrap_hashes[relative]
            ):
                raise ExportError(
                    f"bootstrap artifact hash mismatch: {relative}"
                )
        hashes.sort()
        target_name = plan.configuration.build_target
        manifest = ExportManifest(
            plan_digest=plan.plan_digest,
            target=(
                "" if target_name is None else
                f"{target_name.platform}:{target_name.package_format}"
            ),
            launcher=Path(launcher).as_posix(),
            bootstrap_provider_id=plan.bootstrap_provider.provider_id,
            bootstrap_artifacts=tuple(sorted(
                (
                    artifact.artifact_id,
                    Path(artifact.runtime_path).as_posix(),
                )
                for artifact in bootstrap_artifacts
            )),
            required_capabilities=plan.report.required_capabilities,
            files=tuple(hashes),
        )
        (staging / "dos_re_release.json").write_text(
            json.dumps({
                "schema": RELEASE_MANIFEST_SCHEMA,
                "plan_digest": manifest.plan_digest,
                "target": manifest.target,
                "launcher": manifest.launcher,
                "bootstrap_provider": manifest.bootstrap_provider_id,
                "bootstrap_artifacts": dict(manifest.bootstrap_artifacts),
                "required_capabilities": list(manifest.required_capabilities),
                "files": dict(manifest.files),
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_release_artifact(
    artifact: str | Path,
    command: tuple[str, ...],
    *,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Hash-audit and cold-start a packaged artifact with a scrubbed environment.

    ``command`` is target-specific (for example a Python interpreter plus the
    packaged launcher, or a native executable).  The build system chooses the
    runner; this function supplies the common closed-world proof boundary.
    """
    artifact = Path(artifact).resolve()
    manifest_path = artifact / "dos_re_release.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportError(f"cannot read release manifest: {manifest_path}") from exc
    if payload.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise ExportError(
            f"unsupported release manifest schema: {payload.get('schema')!r}"
        )
    expected = set(payload.get("files", {})) | {"dos_re_release.json"}
    actual = {
        path.relative_to(artifact).as_posix()
        for path in artifact.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ExportError(
            "artifact file closure differs from manifest; missing="
            + repr(sorted(expected - actual))
            + ", unexpected="
            + repr(sorted(actual - expected))
        )
    for relative, expected_digest in payload["files"].items():
        digest = hashlib.sha256((artifact / relative).read_bytes()).hexdigest()
        if digest != expected_digest:
            raise ExportError(f"release file hash mismatch: {relative}")
    if not command:
        raise ExportError("hermetic execution command must not be empty")
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
        }
    }
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=artifact,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"hermetic release execution failed to start: {exc}") from exc
    if completed.returncode:
        raise ExportError(
            f"hermetic release execution exited {completed.returncode}:\n"
            f"{completed.stdout}{completed.stderr}"
        )
    return completed
