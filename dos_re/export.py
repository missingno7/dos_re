"""Closed-world release export for a resolved dos_re execution plan."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .execution import ExecutionPlan


FORBIDDEN_RELEASE_IMPORTS = (
    "ctypes",
    "importlib",
    "pkgutil",
    "runpy",
    "dos_re.cpu",
    "dos_re.cpu386",
    "dos_re.execution",
    "dos_re.hooks",
    "dos_re.player",
    "dos_re.pm_player",
    "dos_re.replay",
    "dos_re.runtime",
    "dos_re.snapshot",
    "dos_re.verification",
    "dos_re.pm_verification",
)


@dataclass(frozen=True)
class ExportFile:
    source: Path
    destination: str


@dataclass(frozen=True)
class ExportManifest:
    plan_digest: str
    target: str
    launcher: str
    files: tuple[tuple[str, str], ...]


class ExportError(RuntimeError):
    pass


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


def _forbidden_import(name: str) -> str | None:
    for forbidden in FORBIDDEN_RELEASE_IMPORTS:
        if name == forbidden or name.startswith(forbidden + "."):
            return forbidden
    return None


def _validate_files(files: tuple[ExportFile, ...], launcher: str) -> None:
    destinations: set[str] = set()
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
        for imported in sorted(_import_names(source, destination)):
            forbidden = _forbidden_import(imported)
            if forbidden:
                raise ExportError(
                    f"{source} imports development-only {forbidden!r} "
                    f"through {imported!r}"
                )
        dynamic_calls = _dynamic_loading_calls(source)
        if dynamic_calls:
            raise ExportError(
                f"{source} uses release-forbidden dynamic loading/evaluation: "
                + ", ".join(dynamic_calls)
            )
    if Path(launcher).as_posix() not in destinations:
        raise ExportError(f"launcher {launcher!r} is not in the export closure")


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
    _validate_files(files, launcher)

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
        target_name = plan.configuration.build_target
        manifest = ExportManifest(
            plan_digest=plan.plan_digest,
            target=(
                "" if target_name is None else
                f"{target_name.platform}:{target_name.package_format}"
            ),
            launcher=Path(launcher).as_posix(),
            files=tuple(hashes),
        )
        (staging / "dos_re_release.json").write_text(
            json.dumps({
                "plan_digest": manifest.plan_digest,
                "target": manifest.target,
                "launcher": manifest.launcher,
                "files": dict(manifest.files),
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
