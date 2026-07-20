"""Build-image consumer and detachment diagnostics for a generated graph backend.

Planning owns bootstrap selection, dependency closure, and detachment claims.
After a validated plan has selected a build-image provider and a generated
implementation graph, a backend activator can use
:func:`boot_generated_graph_image` to construct the machine from those planned
artifacts. The EXE access guard and poisoned-code checks are additional
development evidence; they are not an alternate release authority.
"""
from __future__ import annotations

import builtins
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from .bootimage import BOOT_IMAGE_FORMAT


class GeneratedGraphBootstrapError(RuntimeError):
    """A selected generated-graph backend reached a forbidden dependency."""


@contextmanager
def exe_access_guard(forbidden_name: str, forbidden_sha256: str,
                     forbidden_size: int | None = None):
    """Forbid opening the original binary for the duration of the block.

    Wraps ``builtins.open`` so any read of a file whose basename matches the
    original EXE, OR whose content hashes to the recorded EXE digest (rename
    defence), raises :class:`GeneratedGraphBootstrapError`. Game data files stay
    readable -- only the executable is walled off.  The content hash is computed
    only when the file size matches ``forbidden_size`` (when known), keeping the
    guard cheap on the game's many data-file reads."""
    real_open = builtins.open
    fname = forbidden_name.lower()

    def _hash_with_real_open(p: Path) -> str:
        h = hashlib.sha256()
        with real_open(p, "rb") as fh:   # real_open -- NOT the patched builtin
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def guarded_open(file, mode="r", *args, **kwargs):
        try:
            p = Path(file)
            name = p.name.lower()
        except TypeError:
            return real_open(file, mode, *args, **kwargs)
        if name == fname:
            raise GeneratedGraphBootstrapError(
                "generated-graph backend tried to open the original executable "
                f"{p.name!r}; the selected build image is its only bootstrap source")
        if "b" in mode and ("r" in mode or "+" in mode):
            try:
                if p.is_file() and (forbidden_size is None
                                    or p.stat().st_size == forbidden_size):
                    if _hash_with_real_open(p) == forbidden_sha256:
                        raise GeneratedGraphBootstrapError(
                            f"generated-graph backend tried to open a file whose "
                            f"contents are the original executable ({p} -- "
                            "renamed?); refused")
            except OSError:
                pass
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    try:
        yield
    finally:
        builtins.open = real_open


@contextmanager
def exe_access_guard_from_manifest(manifest: dict):
    """:func:`exe_access_guard` parameterized from a boot manifest's source_exe."""
    src = manifest["source_exe"]
    with exe_access_guard(src["name"], src["sha256"], src.get("size")):
        yield


def load_boot_manifest(boot_dir: Path | str) -> dict:
    """Load and schema-check a generated boot image's manifest."""
    manifest = json.loads((Path(boot_dir) / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != BOOT_IMAGE_FORMAT:
        raise GeneratedGraphBootstrapError(
            f"unrecognized boot image schema in {boot_dir} "
            f"(want {BOOT_IMAGE_FORMAT!r}, got {manifest.get('schema')!r})")
    return manifest


def boot_generated_graph_image(
    boot_dir: Path | str,
    *,
    game_root: Path | str,
    lift_dir: Path | str,
):
    """Activate a selected generated graph from a selected build image.

    Returns ``(runtime, manifest)``. The graph is activated in full and
    interpreter fallback is forbidden. Any partial composition must be
    represented by catalog entries and a different backend activator, never by
    hidden skip/install flags here.
    """
    from .snapshot_runtime import load_snapshot_headless
    from .lift.install import activate_generated_graph

    boot_dir = Path(boot_dir)
    manifest = load_boot_manifest(boot_dir)

    rt = load_snapshot_headless(boot_dir, game_root=str(game_root))
    if rt.program.exe is not None:  # defensive: headless load must not carry an EXE
        raise GeneratedGraphBootstrapError(
            "build-image load carried an executable")
    # The recovered code is zeroed in the image (poison); tell the lifted entry
    # guards not to compare against the (poisoned) live bytes.
    if manifest.get("poison", {}).get("enabled"):
        rt.cpu.code_poisoned = True

    activate_generated_graph(rt.cpu, Path(lift_dir))
    # Hard runtime witness: any uncovered address raises rather than falling
    # back outside the selected implementation graph.
    rt.cpu.interp_forbidden = True
    rt._generated_graph_boot_manifest = manifest
    return rt, manifest


def generated_graph_boot_report(
    manifest: dict, *, exe_present_at_runtime: bool = False,
) -> str:
    """Report runtime facts from an optional detachment diagnostic run."""
    p = manifest.get("poison", {})
    holds = (not exe_present_at_runtime
             and p.get("enabled")
             and p.get("code_bytes_present_after", 1) == 0)
    lines = [
        f"Boot source: generated data-only boot image "
        f"({manifest['artifacts']['memory']} + {manifest['artifacts']['state']})",
        f"Original EXE required at runtime: no "
        f"(source {manifest['source_exe']['name']} sha256 "
        f"{manifest['source_exe']['sha256'][:12]}... consumed at BUILD time only)",
        f"Recovered code poisoned: {p.get('poisoned_bytes', 0)} bytes in "
        f"{p.get('poisoned_runs', 0)} runs; "
        f"code bytes still present: {p.get('code_bytes_present_after', '?')}",
        "Interpreter fallback: forbidden by generated-graph backend",
        f"Runtime detachment evidence: {'HOLDS' if holds else 'DOES NOT HOLD'}",
    ]
    return "\n".join(lines)
