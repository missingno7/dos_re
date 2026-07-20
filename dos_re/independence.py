"""Optional destructive EXE/interpreter-detachment proof tools.

The canonical source of truth is the dependency closure in
``dos_re.execution`` and its closed-world export. This older data-only boot
path remains useful during development because it deliberately destroys
recovered instruction bytes, blocks EXE access, and poisons interpreter
fallback. Passing it is focused supporting evidence, not release readiness.

    The EXE goes into the recovery pipeline.  Generated host code and data
    come out.  The VMless runtime never sees the EXE again.

Pieces:

* :class:`VMlessViolation` -- raised when the runtime touches something the
  wall forbids (the binary, or interpretation via the CPU poison).
* :func:`exe_access_guard` -- wraps ``builtins.open`` for the session: opening
  the original binary by NAME or by CONTENT HASH (rename defence) raises.
* :func:`load_boot_manifest` -- loads + schema-checks a boot-image manifest.
* :func:`boot_vmless_image` -- the one EXE-free boot path: headless image load,
  lifted-graph install, signature-guard bypass for poisoned code, and the
  interpreter poison armed from instruction zero.
* :func:`independence_report` -- a derived report for this optional proof run.

The build-time counterpart (producing the image) is :mod:`dos_re.bootimage`.
"""
from __future__ import annotations

import builtins
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

BOOT_MANIFEST_SCHEMA = "vmless_boot_manifest/v1"


class VMlessViolation(RuntimeError):
    """Raised when the strict-VMless runtime touches something it must never
    touch -- the original executable, or the interpreter (via the CPU poison)."""


@contextmanager
def exe_access_guard(forbidden_name: str, forbidden_sha256: str,
                     forbidden_size: int | None = None):
    """Forbid opening the original binary for the duration of the block.

    Wraps ``builtins.open`` so any read of a file whose basename matches the
    original EXE, OR whose content hashes to the recorded EXE digest (rename
    defence), raises :class:`VMlessViolation`.  Legitimate game DATA files stay
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
            raise VMlessViolation(
                f"strict-VMless runtime tried to open the original executable "
                f"{p.name!r} -- the boot image is the only permitted source "
                f"(dos_re_2.0 section 1a').")
        if "b" in mode and ("r" in mode or "+" in mode):
            try:
                if p.is_file() and (forbidden_size is None
                                    or p.stat().st_size == forbidden_size):
                    if _hash_with_real_open(p) == forbidden_sha256:
                        raise VMlessViolation(
                            f"strict-VMless runtime tried to open a file whose "
                            f"contents are the original executable ({p} -- "
                            f"renamed?); refused (dos_re_2.0 section 1a').")
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
    if manifest.get("schema") != BOOT_MANIFEST_SCHEMA:
        raise VMlessViolation(
            f"unrecognized boot image schema in {boot_dir} "
            f"(want {BOOT_MANIFEST_SCHEMA!r}, got {manifest.get('schema')!r})")
    return manifest


def boot_vmless_image(
    boot_dir: Path | str,
    *,
    game_root: Path | str,
    lift_dir: Path | str,
    skip: set[str] | frozenset[str] = frozenset(),
    install_graph: bool = True,
    arm_wall: bool = True,
):
    """Boot a strict-VMless runtime from a generated data-only image.

    Returns ``(runtime, manifest)``.  ``skip`` is the port's declared
    keep-interpreted set (its automation-gap queue); a strict-VMless boot
    REFUSES to start while it is non-empty -- that is the hard wall gate, and a
    transitional session belongs in the port's hybrid runner instead.

    With ``arm_wall`` (the default) the interpreter poison is armed from the
    first step: the run is VMless from instruction zero because the image is
    already at the canonical post-decompression entry -- there is no loader
    phase to interpret.
    """
    from .snapshot_runtime import load_snapshot_headless
    from .lift.install import install_vmless_graph

    boot_dir = Path(boot_dir)
    manifest = load_boot_manifest(boot_dir)

    rt = load_snapshot_headless(boot_dir, game_root=str(game_root))
    if rt.program.exe is not None:  # defensive: headless load must not carry an EXE
        raise VMlessViolation("boot image load carried an executable -- not headless")
    # The recovered code is zeroed in the image (poison); tell the lifted entry
    # guards not to compare against the (poisoned) live bytes.
    if manifest.get("poison", {}).get("enabled"):
        rt.cpu.code_poisoned = True

    if install_graph:
        if skip:
            raise VMlessViolation(
                "the VMless wall is not satisfied -- "
                f"{len(skip)} routine(s) configured for interpreted execution "
                f"({', '.join(sorted(skip))})")
        install_vmless_graph(rt.cpu, Path(lift_dir))
        rt._vmless_boundary_observers = True

    if arm_wall:
        # THE PHYSICAL WALL: interpretation is now impossible; any uncovered
        # address raises rather than falling back to the interpreter.
        rt.cpu.interp_forbidden = True

    rt._vmless_manifest = manifest
    return rt, manifest


def independence_report(manifest: dict, *, exe_present_at_runtime: bool = False) -> str:
    """The DERIVED hard-gate banner: each line is a fact computed from the boot
    image + this run, not a configuration string (dos_re_2.0 section 1a')."""
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
        "Interpreter fallback: forbidden (wall poison armed)",
        f"EXE-independence wall: {'HOLDS' if holds else 'DOES NOT HOLD'}",
    ]
    return "\n".join(lines)
