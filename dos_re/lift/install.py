"""Backend mechanics for activating one selected generated implementation graph.

Implementation availability and selection belong to
:class:`dos_re.execution.ImplementationCatalog` and
:class:`dos_re.execution.ExecutionPlan`.  This module does not inspect proof
statuses or choose a subset.  After a plan has selected a generated graph, its
backend activator may use :func:`activate_generated_graph` to load that exact
corpus, resolve its generated links, and bind its CPU adapter entries.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from .naming import GraphNaming


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_links(loaded: dict[str, object], emit_dir, naming=None) -> int:
    """Second pass of the two-pass load: bind every module's ``LINKS`` table.

    A LINKED module (``tools/liftlink.py`` — the batch de-VM pass) carries a
    module-level ``LINKS = {"CS:IP": None}`` table, and each linked CALL site
    evaluates ``LINKS["CS:IP"]`` at call time.  The late binding is what keeps
    each emitted module loadable STANDALONE: modules live flat in one directory
    and are loaded via ``spec_from_file_location`` (no package, so sibling
    imports at module top level are impossible).  This pass fills the tables
    with the callees' lifted functions, loading sibling modules from
    ``emit_dir`` as needed — transitively, since a loaded callee may itself be
    linked.  Loud on a missing callee module: a linked caller whose callee is
    not on disk is a pipeline error, never something to skip silently.

    ``naming`` maps entry addresses to module stems (the manifest seam,
    ``dos_re.lift.naming``); ``None`` loads ``graph_manifest.json`` from
    ``emit_dir`` — absent manifest = the historical address-derived names.

    ``loaded`` maps function/module stem name → loaded module and is extended
    in place with every sibling this pass pulls in.  Returns the number of
    link slots bound."""
    emit_dir = Path(emit_dir)
    if naming is None:
        naming = GraphNaming.load(emit_dir)
    pending = list(loaded.values())
    resolved = 0
    while pending:
        mod = pending.pop()
        links = getattr(mod, "LINKS", None)
        if not links:
            continue
        for entry in sorted(links):
            name = naming.stem_of(entry)
            callee = loaded.get(name)
            if callee is None:
                path = emit_dir / f"{name}.py"
                if not path.is_file():
                    raise FileNotFoundError(
                        f"linked callee {entry} of {getattr(mod, '__name__', '?')}: "
                        f"module {path} is missing")
                callee = _load_module(path)
                loaded[name] = callee
                pending.append(callee)          # it may carry links of its own
            links[entry] = getattr(callee, name)
            resolved += 1
    return resolved


def activate_generated_graph(cpu, emit_dir) -> dict[tuple[int, int], str]:
    """Bind every module in one already-selected generated graph.

    This is a CPU-backend activator, not a composition policy.  The owning
    :class:`~dos_re.execution.ImplementationEntry` describes the graph's
    targets, evidence, properties, dependencies, and digest; the immutable plan
    must select it before this function is called.

    Standalone-hook installation is exit-shape-agnostic (a retf/iret entry's
    lifted body reproduces its own far/interrupt return): the near-ret
    restriction only applies to linked generated calls, not to entries reached
    through the CPU adapter and replaced whole. Composition is fixed by the
    selected implementation descriptor; this backend adapter cannot silently
    skip entries and thereby create a second execution plan. Two-pass (load,
    resolve links, register); loud on a missing linked callee.

    Module discovery honours the naming manifest (``dos_re.lift.naming``):
    manifested entries name their modules symbolically (loud when a
    manifested module file is missing — a manifest naming a module that is
    not there is a corrupt selected implementation); default-named
    ``lifted_CS_IP.py`` modules not covered by the manifest are also loaded."""
    emit_dir = Path(emit_dir)
    naming = GraphNaming.load(emit_dir)
    installed: dict[tuple[int, int], str] = {}
    for cs, ip, stem in naming.entries():
        if not (emit_dir / f"{stem}.py").is_file():
            raise FileNotFoundError(
                f"graph manifest entry {cs:04X}:{ip:04X}: module "
                f"{emit_dir / (stem + '.py')} is missing")
        installed[(cs, ip)] = f"{stem}.py"
    for path in sorted(emit_dir.glob("lifted_*.py")):
        stem = path.stem                       # lifted_1010_16a9
        parts = stem.split("_")
        if len(parts) != 3:
            continue
        try:
            cs, ip = int(parts[1], 16), int(parts[2], 16)
        except ValueError:                     # a symbolic stem, not the pattern
            continue
        if (cs, ip) in installed:
            continue
        installed[(cs, ip)] = path.name
    loaded: dict[str, object] = {}
    for key, module in sorted(installed.items()):
        loaded[module[:-3]] = _load_module(emit_dir / module)
    resolve_links(loaded, emit_dir, naming)
    for key, module in sorted(installed.items()):
        name = module[:-3]
        fn = getattr(loaded[name], name)
        cpu.replacement_hooks[key] = fn
        cpu.hook_names[key] = name
        # RESUME ENTRIES (lift/emit ``boundary_heads``): a boundary park
        # re-points CS:IP just past the observed head; registering the
        # module's re-entry hook there makes the resume run INSIDE the lifted
        # body — boundary observation with zero interpreted instructions.
        for entry_key, bb in getattr(loaded[name], "RESUME_ENTRIES", {}).items():
            r_cs, r_ip = (int(x, 16) for x in entry_key.split(":"))

            def _resume(cpu2, _fn=fn, _bb=bb):
                _fn(cpu2, bb=_bb)

            _resume.owns_time = getattr(fn, "owns_time", False)
            cpu.replacement_hooks[(r_cs, r_ip)] = _resume
            cpu.hook_names[(r_cs, r_ip)] = f"{name}_resume_{r_ip:04x}"
    return installed
