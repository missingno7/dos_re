from pathlib import Path
import sys

import pytest

from dos_re.bootstrap_runtime import packaged_bootstrap_artifacts
from dos_re.execution import (
    BootstrapArtifact,
    BootstrapExportMode,
    BuildImageBootstrapProvider,
    BuildTarget,
    DependencyCapability,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    ProgramCoverage,
    plan_execution,
    profile_configuration,
)
from dos_re.export import (
    ExportError,
    ExportFile,
    export_release,
    verify_release_artifact,
)


def _release_plan(*, capabilities=(), bootstrap=None):
    coverage = ProgramCoverage(("root",), frozenset({"root"}), evidence_identity="v1")
    catalog = ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
        "product", frozenset({"root"}), ImplementationOrigin.GENERATED,
        required_capabilities=frozenset(capabilities),
        implementation_digest="product-v1",
    )),))
    return plan_execution(
        profile_configuration(
            "release", program_identity="game",
            bootstrap_provider=bootstrap,
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
    with pytest.raises(ExportError, match="outside the selected release closure"):
        export_release(
            _release_plan(),
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_export_rejects_from_import_of_development_runtime(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("from dos_re import replay\n", encoding="utf-8")
    with pytest.raises(ExportError, match="outside the selected release closure"):
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


def test_export_rejects_dynamic_runtime_module(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("import ctypes\n", encoding="utf-8")
    with pytest.raises(ExportError, match="dynamic runtime module"):
        export_release(
            _release_plan(),
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_export_ignores_annotation_only_development_imports(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from dos_re.cpu import CPU8086\n"
        "print('product')\n",
        encoding="utf-8",
    )
    export_release(
        _release_plan(),
        (ExportFile(launcher, "launch.py"),),
        tmp_path / "dist",
        launcher="launch.py",
    )


def test_export_resolves_forbidden_relative_imports(tmp_path: Path):
    module = tmp_path / "product.py"
    module.write_text("from . import player\n", encoding="utf-8")
    with pytest.raises(ExportError, match="outside the selected release closure"):
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


def test_export_allows_runtime_dependency_present_in_selected_closure(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("import dos_re.cpu\n", encoding="utf-8")
    export_release(
        _release_plan(capabilities=(DependencyCapability.CPU_MODEL.value,)),
        (ExportFile(
            launcher,
            "launch.py",
            frozenset({DependencyCapability.CPU_MODEL.value}),
        ),),
        tmp_path / "dist",
        launcher="launch.py",
    )


def test_export_treats_headless_state_restore_as_product_runtime(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "from dos_re.snapshot_headless import _restore_dos_state\n",
        encoding="utf-8",
    )
    export_release(
        _release_plan(capabilities=(DependencyCapability.DOS_RE_RUNTIME.value,)),
        (ExportFile(
            launcher,
            "launch.py",
            frozenset({DependencyCapability.DOS_RE_RUNTIME.value}),
        ),),
        tmp_path / "dist",
        launcher="launch.py",
    )


def test_export_rejects_file_for_detached_component(tmp_path: Path):
    launcher = tmp_path / "launch.py"
    launcher.write_text("print('game')\n", encoding="utf-8")
    with pytest.raises(ExportError, match="tagged with capabilities outside"):
        export_release(
            _release_plan(),
            (ExportFile(
                launcher,
                "launch.py",
                frozenset({DependencyCapability.CPU_MODEL.value}),
            ),),
            tmp_path / "dist",
            launcher="launch.py",
        )


def test_hermetic_proof_runs_only_manifest_closure_without_workspace_imports(
    tmp_path: Path,
):
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert 'PYTHONPATH' not in os.environ\n"
        "assert not Path('dos_re/cpu.py').exists()\n"
        "print('detached')\n",
        encoding="utf-8",
    )
    output = tmp_path / "dist"
    export_release(
        _release_plan(),
        (ExportFile(launcher, "launch.py"),),
        output,
        launcher="launch.py",
    )
    completed = verify_release_artifact(
        output,
        (sys.executable, "-I", "launch.py"),
    )
    assert completed.stdout.strip() == "detached"


def test_export_requires_exact_declared_asset_closure(tmp_path: Path):
    coverage = ProgramCoverage(("root",), frozenset({"root"}), evidence_identity="v1")
    catalog = ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
        "product",
        frozenset({"root"}),
        ImplementationOrigin.GENERATED,
        required_assets=frozenset({"levels"}),
    )),))
    plan = plan_execution(
        profile_configuration(
            "release",
            program_identity="game",
            build_target=BuildTarget("windows", "directory"),
        ),
        coverage,
        catalog,
    )
    launcher = tmp_path / "launch.py"
    launcher.write_text("print('game')\n", encoding="utf-8")
    levels = tmp_path / "levels.dat"
    levels.write_bytes(b"levels")
    with pytest.raises(ExportError, match="missing=.*levels"):
        export_release(
            plan,
            (ExportFile(launcher, "launch.py"),),
            tmp_path / "missing",
            launcher="launch.py",
        )
    export_release(
        plan,
        (
            ExportFile(launcher, "launch.py"),
            ExportFile(levels, "assets/levels.dat", asset_id="levels"),
        ),
        tmp_path / "complete",
        launcher="launch.py",
    )


def test_export_materializes_declared_bootstrap_artifacts(tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text('{"boot": true}\n', encoding="utf-8")
    bootstrap = BuildImageBootstrapProvider(
        "image",
        ("machine state",),
        artifacts=(BootstrapArtifact(
            "state",
            "bootstrap/state.json",
            str(state),
        ),),
        valid_profiles=frozenset({"release"}),
    )
    launcher = tmp_path / "launch.py"
    launcher.write_text("print('game')\n", encoding="utf-8")
    output = tmp_path / "dist"
    manifest = export_release(
        _release_plan(bootstrap=bootstrap),
        (ExportFile(launcher, "launch.py"),),
        output,
        launcher="launch.py",
    )
    assert (output / "bootstrap" / "state.json").read_bytes() == state.read_bytes()
    assert manifest.bootstrap_provider_id == "image"
    assert packaged_bootstrap_artifacts(
        output,
        expected_provider="image",
    )["state"] == output / "bootstrap" / "state.json"
    payload = (output / "dos_re_release.json").read_text(encoding="utf-8")
    assert '"bootstrap_provider": "image"' in payload


def test_export_can_generate_bootstrap_artifact(tmp_path: Path):
    bootstrap = BuildImageBootstrapProvider(
        "generated-image",
        ("native state",),
        artifacts=(BootstrapArtifact(
            "generated-state",
            "bootstrap/state.bin",
            export_mode=BootstrapExportMode.GENERATE,
            materializer=lambda path: path.write_bytes(b"generated"),
        ),),
        valid_profiles=frozenset({"release"}),
    )
    launcher = tmp_path / "launch.py"
    launcher.write_text("print('game')\n", encoding="utf-8")
    output = tmp_path / "dist"
    export_release(
        _release_plan(bootstrap=bootstrap),
        (ExportFile(launcher, "launch.py"),),
        output,
        launcher="launch.py",
    )
    assert (output / "bootstrap" / "state.bin").read_bytes() == b"generated"
