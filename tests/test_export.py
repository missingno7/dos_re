from pathlib import Path

import pytest

from dos_re.execution import (
    BuildTarget,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    ProgramCoverage,
    plan_execution,
    profile_configuration,
)
from dos_re.export import ExportError, ExportFile, export_release


def _release_plan():
    coverage = ProgramCoverage(("root",), frozenset({"root"}), evidence_identity="v1")
    catalog = ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
        "product", frozenset({"root"}), ImplementationOrigin.GENERATED,
        implementation_digest="product-v1",
    )),))
    return plan_execution(
        profile_configuration(
            "release", program_identity="game",
            build_target=BuildTarget("windows", "directory"),
        ),
        coverage,
        catalog,
    )


def test_closed_world_export_binds_files_to_plan(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("print('game')\n", encoding="utf-8")
    output = tmp_path / "dist"
    manifest = export_release(
        _release_plan(),
        (ExportFile(launcher, "launch.py"),),
        output,
        launcher="launch.py",
    )
    assert (output / "launch.py").is_file()
    assert (output / "dos_re_release.json").is_file()
    assert manifest.plan_digest == _release_plan().plan_digest


def test_export_rejects_development_runtime_import(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("import dos_re.player\n", encoding="utf-8")
    with pytest.raises(ExportError, match="development-only"):
        export_release(
            _release_plan(),
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_export_rejects_from_import_of_development_runtime(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("from dos_re import replay\n", encoding="utf-8")
    with pytest.raises(ExportError, match="development-only"):
        export_release(
            _release_plan(),
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_export_rejects_dynamic_loading(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("__import__('product.plugin')\n", encoding="utf-8")
    with pytest.raises(ExportError, match="dynamic loading"):
        export_release(
            _release_plan(),
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_export_resolves_forbidden_relative_imports(tmp_path: Path):
    module = tmp_path / "product.py"
    module.write_text("from . import player\n", encoding="utf-8")
    with pytest.raises(ExportError, match="development-only"):
        export_release(
            _release_plan(),
            (ExportFile(module, "dos_re/product.py"),),
            tmp_path / "dist",
            launcher="dos_re/product.py",
        )


def test_export_requires_release_profile(tmp_path: Path):
    coverage = ProgramCoverage(("root",), frozenset({"root"}))
    catalog = ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
        "product", frozenset({"root"}), ImplementationOrigin.GENERATED,
    )),))
    plan = plan_execution(
        profile_configuration("detached", program_identity="game"),
        coverage,
        catalog,
    )
    launcher = tmp_path / "launch.py"
    launcher.write_text("pass\n", encoding="utf-8")
    with pytest.raises(ExportError, match="release-profile"):
        export_release(
            plan, (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist", launcher="launch.py",
        )
