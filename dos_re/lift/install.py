"""Install ORACLE_PASSING lifted routines as live replacement hooks.

The hybrid-acceleration rung of the recovery ladder (docs/lifting_design.md
§7: LIFTED → ORACLE_PASSING → INSTALLED → REFACTORED).  Once a lifted routine
is proven byte-exact against the oracle, running it as the replacement is a
free speed-up long before ``play_native.py`` exists: interpreted code reaches
the proven routine, runs the Python replacement, returns to the VM.  As more
routines pass, more of the game moves out of interpretation automatically.

THE DETERMINISM CONTRACT (why this module also fingerprints):
Installing a hook changes work-per-``step()`` — a hook that replaces a 200-
instruction routine costs ONE ``step()``.  A demo whose clock is the frame
index (N steps/frame) therefore desyncs if replayed under a *different* hook
set than it was recorded under.  So an installed set carries a **fingerprint**
(``lift_fingerprint``); the play runner records it in the demo and refuses to
replay a demo under a mismatched set (fail loud, never silent desync — the
charter's one-boundary-definition rule).  A demo recorded hook-free has an
empty fingerprint and replays hook-free.

This module is game-agnostic: it is handed a manifest + the directory the
lifted modules live in (both produced by ``tools/liftverify.py``), and the
game's code segment.  It knows nothing about any specific routine.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

#: statuses whose lifted module is proven safe to run as the replacement.
INSTALLABLE_STATUSES = ("ORACLE_PASSING", "INSTALLED")


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def passing_entries(manifest_paths, *, statuses=INSTALLABLE_STATUSES) -> dict[str, str]:
    """Best proven module per entry across manifests → {"CS:IP": module_name}.

    Merges several proof passes (menu/gameplay/demo drives): an entry counts as
    installable if ANY pass proved it, and the last-seen module name wins (all
    passes emit the same deterministic module for an entry)."""
    keep: dict[str, str] = {}
    for mpath in manifest_paths:
        p = Path(mpath)
        if not p.is_file():
            continue
        data = json.loads(p.read_text())
        recs = list(data.values()) if isinstance(data, dict) else data
        for r in recs:
            if r.get("status") in statuses and r.get("module"):
                keep[r["entry"]] = r["module"]
    return keep


def planned_lifts(cs: int, manifest_paths, *, skip=()) -> dict[tuple[int, int], str]:
    """The set that WOULD be installed → {(cs,ip): module_name}.  Pure (no cpu,
    no file loads) so the play runner can fingerprint the plan before booting,
    and install exactly the same set.  ``skip`` = "CS:IP" strings to exclude."""
    skip = set(skip)
    plan: dict[tuple[int, int], str] = {}
    for entry, module in passing_entries(manifest_paths).items():
        if entry in skip:
            continue
        e_cs, e_ip = (int(x, 16) for x in entry.split(":"))
        if e_cs == (cs & 0xFFFF):
            plan[(e_cs, e_ip)] = module
    return plan


def install_passing_lifts(cpu, cs: int, emit_dir, manifest_paths, *,
                          skip=()) -> dict[tuple[int, int], str]:
    """Install every proven lifted routine in segment ``cs`` as a replacement.

    Returns {(cs,ip): module_name} for the installed set.  Loud on a missing
    module file — a manifest referencing a module that isn't there is a
    pipeline error, not something to skip silently."""
    emit_dir = Path(emit_dir)
    installed = planned_lifts(cs, manifest_paths, skip=skip)
    for key, module in sorted(installed.items()):
        mod = _load_module(emit_dir / module)
        fn = getattr(mod, module[:-3])          # "lifted_1010_1550.py" → fn "lifted_1010_1550"
        cpu.replacement_hooks[key] = fn
        cpu.hook_names[key] = module[:-3]
    return installed


def lift_fingerprint(installed: dict[tuple[int, int], str]) -> str:
    """A deterministic fingerprint of an installed lifted set.

    Covers BOTH which addresses are hooked and which module answers each — so
    a re-lift that changes a routine's body (new sha) also changes the
    fingerprint and forces demos to be re-validated.  Empty set → "" (a
    hook-free demo)."""
    if not installed:
        return ""
    payload = ";".join(f"{cs:04X}:{ip:04X}={mod}"
                       for (cs, ip), mod in sorted(installed.items()))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]
