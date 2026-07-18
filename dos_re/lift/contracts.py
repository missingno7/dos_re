"""ABI-contract inference -- the M3b (ABI-recovered CPUless) analysis.

Stage 2b (docs/dos_re_2.0.md) removes the historical CPU CALLING CONVENTION
from the public recovered contracts while the memory image stays
authoritative.  This module supplies the deterministic, game-agnostic
inference the transformation is driven by -- over the shared recovery IR and
the composed CPUless call graph, never by parsing generated Python:

  * :func:`compose_effects` -- interprocedural (inputs, outputs) per function
    to a bottom-up fixpoint (mirrors tools/cpuless_promote.py's rounds: a
    function composes once every direct callee has composed; direct
    self-recursion takes the conservative inductive self-contract; anything
    left in a cycle or behind an unresolved callee composes CONSERVATIVELY
    and is reported as such -- the census refuses it, never a silent
    register-shaped fallback);
  * :func:`observed_returns` -- interprocedural exit-liveness narrowing: a
    register output belongs to the RECOVERED contract only when some caller
    actually observes it after some call site (backward liveness in each
    caller, exit-seeded with the caller's own observed set, to a decreasing
    fixpoint).  Externally-reachable functions (roots, dynamic-dispatch and
    vector targets, functions with no static caller) keep their full
    mechanical output set, flagged conservative;
  * :func:`stack_args_of` -- callee-side stack-argument evidence: return
    discipline (near/far/iret, uniform ``ret N``), the standard frame
    prologue (``push bp; mov bp,sp`` or ``enter``), and the ``[bp+disp]``
    argument slots actually accessed above the return address;
  * :func:`pointer_pairs_of` -- (segment, register) pairs jointly used in
    effective addresses / string ops: the evidence for promoting register
    pairs to pointer-like values or typed memory views;
  * :func:`infer_contracts` -- the corpus census: one
    :class:`ContractProposal` per function, classifying every element as
    SEMANTIC (public API), MACHINE (private stack/frame machinery kept out
    of the API), or dropped (mechanical outputs no caller observes), with
    per-field evidence and structured refusals.

Refusal-first: a conflicting or uncertain contract REFUSES with named
evidence.  The proposals here are analysis output -- the ABI-recovered
emitter consumes them; the mechanical CPUless graph remains the generated
reference for differential verification.
"""
from __future__ import annotations

from dataclasses import dataclass

from .decode import CALL, CALL_FAR, RET, RETF, IRET
from .cpuless import (ALL_REGS, Effects, W16, SEGS, _EA_REGS, _successors,
                      abi_scan, register_effects)
from .ir import apply_desmc, desmc_operand_slots, scan_from_ir_record

#: registers that are pure stack machinery in a recovered contract: never
#: semantic parameters, never semantic returns (an sp-carrying contract is
#: reported, not silently accepted).
MACHINE_REGS = frozenset({"sp", "ss"})

#: the conservative register bundle assumed for an uncomposable callee
#: (mirrors the runtime-dispatch contract in cpuless.register_effects).
_CONS_READS = frozenset(W16) | frozenset({"ds", "es", "ss"})
_CONS_WRITES = _CONS_READS - frozenset({"ss", "sp"})

#: the bundle a boundary observer digests at a park (emit_cpuless._DYN_REGS
#: + the flags word): the acceptance harness compares the REGISTER FILE at
#: every tick boundary, so a park site observes all of these.
_OBSERVER_REGS = frozenset(W16) | frozenset({"ds", "es", "ss"})


def scan_for(rec: dict):
    """``(FunctionScan, None)`` for one IR record -- de-SMC applied when the
    record is a desmc-candidate with immediate-only patch slots -- or
    ``(None, reason)`` when the record is not analyzable."""
    slots = desmc_operand_slots(rec)
    if slots is None:
        return None, "ir-not-liftable"
    if any(s[0] != "imm" for s in slots.values()):
        return None, "desmc-unsupported-field"
    scan = scan_from_ir_record(rec)
    if slots:
        apply_desmc(scan, slots)
    return scan, None


#: push/pop-family opcodes: if a function has NONE of these, it never uses
#: ss as a stack -- any ss-addressed access is a DATA-segment selector.
_STACK_FAMILY_OPS = (frozenset(range(0x50, 0x60)) | frozenset({0x06, 0x0E,
                     0x16, 0x1E, 0x07, 0x17, 0x1F, 0x60, 0x61, 0x68, 0x6A,
                     0x9C, 0x9D, 0xC8, 0xC9}))


def ss_is_data_segment(scan) -> bool:
    """True when this function uses ``ss`` ONLY as an ordinary segment
    selector for memory operands, never as a stack.

    The small-model DOS idiom: with SS == DS the assembler emits ``ss:``
    overrides on ordinary global accesses (``mov ss:[0x0006], ax``).  Such a
    function has no push/pop traffic at all, so its ``ss`` is a semantic
    SEGMENT input like ds/es -- not the machine stack carrier, and treating
    it as machine-private would wrongly hide a real parameter.

    Only an EXPLICIT ``ss:`` prefix counts as data-selector evidence.  A
    bp-based EA that merely DEFAULTS to SS (``mov ax,[bp+2]``) is the classic
    frame-access idiom -- bp may hold a frame pointer established by the
    caller -- so it disqualifies the function instead.

    Refusal-first: a function that does BOTH (real stack traffic AND
    ss-addressed data) keeps ss classified as machine, because there the
    stack and the data genuinely share one segment.  A CALL counts as stack
    traffic for this purpose: the mechanical composition writes the return
    address through ``ss:sp``, so the same segment carries both the frame
    and the data and the two uses cannot be told apart."""
    uses_ss_override = False
    for i in scan.insts.values():
        if i.op in _STACK_FAMILY_OPS or i.kind in (CALL, CALL_FAR):
            return False                      # real stack traffic
        if any(p == 0x36 for p in i.prefixes):
            uses_ss_override = True
            continue                          # explicit: a segment selector
        if i.modrm is not None and i.mod != 3 and i.rm in (2, 3, 6) \
                and not (i.mod == 0 and i.rm == 6) \
                and not any(p in (0x26, 0x2E, 0x3E) for p in i.prefixes):
            return False                      # frame-shaped bp EA (SS default)
    return uses_ss_override


def build_scans(ir: dict) -> tuple[dict, dict]:
    """(scans, skipped) over the corpus: ``scans[key]`` a FunctionScan;
    ``skipped[key]`` the reason a record could not be scanned."""
    scans: dict[str, object] = {}
    skipped: dict[str, str] = {}
    for key, rec in sorted(ir["functions"].items()):
        scan, why = scan_for(rec)
        if scan is None:
            skipped[key] = why
        else:
            scans[key] = scan
    return scans, skipped


# ---------------------------------------------------------------------------
# interprocedural composition: (inputs, outputs) per function, to fixpoint

def _call_sites(scan, cs: int):
    """Direct call sites of one scan: ``[(ip, kind, target_key)]`` with
    ``target_key`` the canonical "CS:IP" of the static target."""
    sites = []
    for i in scan.insts.values():
        if i.kind == CALL and i.target is not None:
            sites.append((i.ip, "near", f"{cs:04X}:{i.target:04X}"))
        elif i.kind == CALL_FAR and i.far_target is not None:
            ts, to = i.far_target
            sites.append((i.ip, "far", f"{ts:04X}:{to:04X}"))
    return sites


def compose_effects(ir: dict, scans: dict, *,
                    widen_full: frozenset = frozenset()) -> tuple[dict, dict,
                                                                  dict]:
    """Interprocedural (inputs, outputs) per function key, composed bottom-up
    over the direct call graph to fixpoint.

    ``widen_full``: keys whose INPUTS widen to the full register bundle --
    functions containing a boundary head or a dynamic-arrival address, where
    liveness at the observer/arrival is externally governed.  Mirrors
    emit_cpuless.check_promotable's widening, so the composed contracts here
    agree with what the mechanical emitter actually shipped.

    Returns ``(effects, reports, notes)``: ``effects[key] = (inputs,
    outputs)`` frozensets, ``reports[key]`` the underlying
    :class:`~dos_re.lift.cpuless.AbiReport`, and ``notes[key]`` composition
    caveats ("self-recursive", "conservative-composition", "dead-call-stub",
    "inputs-widened-observer").  A direct target absent from the IR corpus is
    a runtime-dead stub (empty effects, matching the shipped mechanical
    graph)."""
    corpus = frozenset(ir["functions"])
    effects: dict[str, tuple] = {}
    reports: dict[str, object] = {}
    notes: dict[str, list[str]] = {}

    def callee_maps(scan, cs, *, conservative_missing):
        near: dict[int, tuple] = {}
        far: dict[tuple, tuple] = {}
        for ip, kind, tkey in _call_sites(scan, cs):
            if tkey in effects:
                eff = effects[tkey]
            elif tkey not in corpus:
                eff = (frozenset(), frozenset())      # dead-call stub
            elif conservative_missing:
                eff = (_CONS_READS, _CONS_WRITES)
            else:
                return None, None                     # callee not yet composed
            if kind == "near":
                near[int(tkey.split(":")[1], 16)] = eff
            else:
                far[tuple(int(x, 16) for x in tkey.split(":"))] = eff
        return near, far

    def note(key, msg):
        ns = notes.setdefault(key, [])
        if msg not in ns:
            ns.append(msg)

    while True:
        progress = False
        for key, scan in sorted(scans.items()):
            if key in effects:
                continue
            cs = int(key.split(":")[0], 16)
            sites = _call_sites(scan, cs)
            self_rec = any(t == key for _, _, t in sites)
            if self_rec:
                # conservative inductive seed for the self-call (the same
                # fixed point tools/cpuless_promote.py injects)
                effects[key] = (_CONS_READS, _CONS_WRITES)
            near, far = callee_maps(scan, cs, conservative_missing=False)
            if near is None:
                if self_rec:
                    del effects[key]
                continue
            r = abi_scan(scan, callee_effects=near, far_callee_effects=far)
            ins = r.inputs
            if key in widen_full:
                ins = ins | frozenset(W16) | frozenset({"ds", "es", "ss"})
                note(key, "inputs-widened-observer")
            effects[key] = (ins, r.outputs)
            reports[key] = r
            if self_rec:
                note(key, "self-recursive")
            if any(t not in corpus for _, _, t in sites):
                note(key, "dead-call-stub")
            progress = True
        if not progress:
            break
    # leftovers: cycles / unresolved callees -> conservative composition,
    # loudly noted (the census turns this into a refusal).
    for key, scan in sorted(scans.items()):
        if key in effects:
            continue
        cs = int(key.split(":")[0], 16)
        near, far = callee_maps(scan, cs, conservative_missing=True)
        r = abi_scan(scan, callee_effects=near, far_callee_effects=far)
        ins = r.inputs
        if key in widen_full:
            ins = ins | frozenset(W16) | frozenset({"ds", "es", "ss"})
            note(key, "inputs-widened-observer")
        effects[key] = (ins, r.outputs)
        reports[key] = r
        note(key, "conservative-composition")
    return effects, reports, notes


# ---------------------------------------------------------------------------
# interprocedural exit liveness: which outputs a caller actually observes

def observer_effects(effs: dict, observer_ips: frozenset) -> dict:
    """Overlay boundary-observer semantics onto per-instruction effects: a
    park site hands the full live bundle to ``plat.boundary`` (digested
    against the oracle) and merges the resumed bundle back -- so for
    liveness it READS and WRITES the whole observer bundle.  Applied to the
    ips named in ``observer_ips`` (boundary heads inside this function)."""
    out = dict(effs)
    for ip in observer_ips:
        if ip not in out:
            continue
        e = out[ip]
        out[ip] = Effects(reads=e.reads | _OBSERVER_REGS,
                          writes=e.writes | (_OBSERVER_REGS
                                             - frozenset({"ss"})),
                          mem_read=True, mem_write=e.mem_write,
                          stack_delta=e.stack_delta,
                          refusal=e.refusal, port_io=e.port_io,
                          int_effect=e.int_effect)
    return out


def _live_out_map(scan, effs, exit_seed: frozenset) -> dict[int, frozenset]:
    """Per-instruction live-AFTER register sets by backward may-liveness --
    the same fixpoint as :func:`~dos_re.lift.cpuless.abi_scan`, with the
    exit seed a parameter (the narrowing hook its ``exit_live`` docstring
    reserves).  A NON-return terminal (a tail jmp_far/jmp_ind, or a jmp
    leaving the scanned region) seeds live-ALL: its live-out is governed by
    the transfer target, not by this body, so it must never enable
    narrowing."""
    succ = _successors(scan)
    live_out: dict[int, frozenset] = {}
    for ip in effs:
        if scan.insts[ip].kind in (RET, RETF, IRET):
            live_out[ip] = exit_seed
        elif not succ[ip]:
            live_out[ip] = ALL_REGS
        else:
            live_out[ip] = frozenset()
    changed = True
    while changed:
        changed = False
        for i in sorted(scan.insts.values(), key=lambda x: -x.ip):
            if not succ[i.ip]:
                continue
            out = frozenset().union(*(
                (live_out[s] - effs[s].writes) | effs[s].reads
                for s in succ[i.ip]))
            if out != live_out[i.ip]:
                live_out[i.ip] = out
                changed = True
    return live_out


def observed_returns(ir: dict, scans: dict, effects: dict, *,
                     external: frozenset = frozenset(),
                     observers: dict | None = None) -> tuple[dict, dict]:
    """Narrow every function's register outputs to the CALLER-OBSERVED set.

    ``observed[key]`` = union over all direct call sites of the registers
    live after the call (in the caller's own dataflow, exit-seeded with the
    caller's observed set) intersected with the callee's mechanical outputs.
    Functions in ``external`` -- roots, dynamic-dispatch/vector targets,
    anything reachable outside the static graph -- and functions with NO
    static call site keep their full output set, flagged conservative.

    ``observers``: per-key boundary-head ips inside that function -- a park
    digests the full register bundle against the oracle, so those sites
    read/write everything in the liveness (see :func:`observer_effects`).

    Decreasing fixpoint from the all-outputs seed (exit seeds only shrink,
    so liveness and observation only shrink; terminates).  Returns
    ``(observed, status)`` with ``status[key]`` one of "narrowed" |
    "external-conservative" | "no-static-caller"."""
    observers = observers or {}
    callers: dict[str, list] = {k: [] for k in scans}
    for ckey, scan in sorted(scans.items()):
        cs = int(ckey.split(":")[0], 16)
        for ip, _, tkey in _call_sites(scan, cs):
            if tkey in callers:
                callers[tkey].append((ckey, ip))

    def caller_effs(ckey):
        scan = scans[ckey]
        cs = int(ckey.split(":")[0], 16)
        effs = {i.ip: register_effects(i) for i in scan.insts.values()}
        for ip, _, tkey in _call_sites(scan, cs):
            if tkey in effects:
                ins, outs = effects[tkey]
            else:                                     # dead-call stub
                ins, outs = frozenset(), frozenset()
            effs[ip] = Effects(reads=frozenset(ins) | frozenset({"sp", "ss"}),
                               writes=frozenset(outs) | frozenset({"sp"}),
                               mem_read=True, mem_write=True, stack_delta=0)
        return observer_effects(effs, observers.get(ckey, frozenset()))

    effs_cache = {k: caller_effs(k) for k in sorted(scans)}
    # SEMANTIC registers only: sp/ss are stack machinery in every contract,
    # handled by the depth analysis -- never narrowed, never returned.
    observed = {k: effects[k][1] - MACHINE_REGS for k in scans}
    status = {}
    for k in sorted(observed):
        if k in external:
            status[k] = "external-conservative"
        elif not callers[k]:
            status[k] = "no-static-caller"
        else:
            status[k] = "narrowed"

    changed = True
    while changed:
        changed = False
        live_maps = {ckey: _live_out_map(scans[ckey], effs_cache[ckey],
                                         observed[ckey])
                     for ckey in sorted(scans)}
        for key in sorted(observed):
            if status[key] != "narrowed":
                continue
            outs = effects[key][1] - MACHINE_REGS
            seen: frozenset = frozenset()
            for ckey, ip in callers[key]:
                seen |= live_maps[ckey][ip] & outs
            if seen != observed[key]:
                observed[key] = seen
                changed = True
    return observed, status


# ---------------------------------------------------------------------------
# callee-side stack arguments + return discipline

@dataclass
class StackReport:
    ret_kind: str                 # "near" | "far" | "iret" | "none" | "mixed"
    ret_pop: int | None = 0       # uniform ret N bytes; None = varies (refusal)
    framed: bool = False          # standard prologue proven at entry
    arg_slots: tuple = ()         # sorted (byte offset from first arg, width)
    arg_bytes: int = 0            # max(frame-read extent, ret N)
    refusals: tuple = ()          # named stack-evidence conflicts


def stack_args_of(scan) -> StackReport:
    """Callee-side stack-argument evidence for one function scan.

    A function is FRAMED when its entry opens with the standard prologue
    (``push bp; mov bp,sp``, split or as ``enter``); its ``[bp+disp]``
    accesses with ``disp >= base`` (4 near / 6 far: past saved bp + return
    address) are the argument slots.  ``ret N`` is the callee-clean cleanup.
    Conflicts refuse: mixed near/far returns, a varying ``ret N``, dynamic
    frame indexing (``[bp+si]``/``[bp+di]`` while framed), or an argument
    extent exceeding a nonzero ``ret N``."""
    rets = [i for i in scan.insts.values() if i.kind in (RET, RETF, IRET)]
    kinds = {i.kind for i in rets}
    refusals: list[str] = []
    if not rets:
        ret_kind = "none"
    elif len(kinds) > 1:
        ret_kind = "mixed"
        refusals.append("mixed-return-convention")
    else:
        ret_kind = {RET: "near", RETF: "far", IRET: "iret"}[next(iter(kinds))]
    pops = sorted({(i.imm or 0) if i.op in (0xC2, 0xCA) else 0 for i in rets})
    ret_pop: int | None = pops[0] if len(pops) == 1 else (0 if not pops
                                                         else None)
    if ret_pop is None:
        refusals.append("variable-ret-pop")

    entry = scan.insts.get(scan.entry)
    nxt = scan.insts.get(entry.next_ip) if entry is not None else None
    framed = bool(
        entry is not None
        and (entry.op == 0xC8                                    # enter
             or (entry.op == 0x55 and nxt is not None            # push bp
                 and register_effects(nxt).frame_establish)))    # mov bp,sp

    slots: set[tuple[int, int]] = set()
    extent = 0
    if framed and ret_kind in ("near", "far"):
        base = 4 if ret_kind == "near" else 6
        for i in scan.insts.values():
            if i.modrm is None or i.mod not in (1, 2):
                continue
            if i.rm in (2, 3):                       # [bp+si]/[bp+di]
                if "frame-dynamic-indexing" not in refusals:
                    refusals.append("frame-dynamic-indexing")
                continue
            if i.rm != 6:                            # not bp-based
                continue
            seg_override = any(p in (0x26, 0x2E, 0x36, 0x3E)
                               for p in i.prefixes)
            if seg_override:
                continue                             # not a frame access
            disp = i.disp or 0
            if disp < base:
                continue                             # locals / saved regs
            width = 4 if i.op in (0xC4, 0xC5) else (2 if (i.op & 1) else 1)
            slots.add((disp - base, width))
        if slots:
            extent = max(off + w for off, w in slots)
            if ret_pop not in (None, 0) and extent > ret_pop:
                refusals.append("frame-arg-extent-exceeds-ret-pop")
    arg_bytes = max(extent, ret_pop or 0)
    return StackReport(ret_kind=ret_kind, ret_pop=ret_pop, framed=framed,
                       arg_slots=tuple(sorted(slots)), arg_bytes=arg_bytes,
                       refusals=tuple(refusals))


# ---------------------------------------------------------------------------
# pointer-pair evidence

#: string-op implicit (segment, register) uses; a segment override applies
#: to the SOURCE (ds:si) side only -- es:di is architectural.
_STRING_PAIRS = {
    0xA4: (("ds", "si"), ("es", "di")), 0xA5: (("ds", "si"), ("es", "di")),
    0xA6: (("ds", "si"), ("es", "di")), 0xA7: (("ds", "si"), ("es", "di")),
    0xAA: (("es", "di"),), 0xAB: (("es", "di"),),
    0xAC: (("ds", "si"),), 0xAD: (("ds", "si"),),
    0xAE: (("es", "di"),), 0xAF: (("es", "di"),),
}


def _ea_segment(i) -> str:
    for p in i.prefixes:
        if p in (0x26, 0x2E, 0x36, 0x3E):
            return SEGS[(p >> 3) & 3]
    if i.modrm is not None and i.mod != 3 \
            and (i.rm in (2, 3) or (i.rm == 6 and i.mod != 0)):
        return "ss"
    return "ds"


def pointer_pairs_of(scan, inputs: frozenset) -> dict[tuple[str, str], int]:
    """(segment, register) pairs jointly used to address memory, counted by
    site, restricted to pairs where BOTH members are contract inputs -- the
    evidence for a pointer-like parameter or typed memory view."""
    pairs: dict[tuple[str, str], int] = {}

    def hit(seg: str, reg: str) -> None:
        if seg in inputs and reg in inputs:
            pairs[(seg, reg)] = pairs.get((seg, reg), 0) + 1

    for i in scan.insts.values():
        if i.op in _STRING_PAIRS:
            for seg, reg in _STRING_PAIRS[i.op]:
                if seg == "ds":
                    seg = _ea_segment(i) if i.prefixes else "ds"
                hit(seg, reg)
            continue
        if i.op == 0xD7:                              # xlat seg:[bx+al]
            hit(_ea_segment(i), "bx")
            continue
        if i.modrm is None or i.mod == 3 \
                or (i.mod == 0 and i.rm == 6):
            continue                                  # no base/index regs
        for reg in _EA_REGS[i.rm]:
            hit(_ea_segment(i), reg)
    return pairs


# ---------------------------------------------------------------------------
# the census: one contract proposal per function

#: opcodes that move/stack a segment register as DATA (not as an EA segment).
_SEG_DATA_OPS = frozenset({0x8C, 0x8E, 0x06, 0x0E, 0x16, 0x1E,
                           0x07, 0x17, 0x1F})


@dataclass
class ContractProposal:
    key: str
    params: tuple = ()            # ({"reg","kind","ea_sites","other_sites"},)
    stack: StackReport | None = None
    returns: tuple = ()           # semantic register returns (observed)
    returns_status: str = ""      # "narrowed"|"external-conservative"|...
    dropped_outputs: tuple = ()   # mechanical outputs no caller observes
    machine_private: tuple = ()   # stack/frame machinery kept out of the API
    pointer_pairs: tuple = ()     # (("ds","si",count), ...)
    notes: tuple = ()
    refusals: tuple = ()          # structured {reason, evidence}

    def as_json(self) -> dict:
        d = {"key": self.key, "params": list(self.params),
             "returns": list(self.returns),
             "returns_status": self.returns_status,
             "dropped_outputs": list(self.dropped_outputs),
             "machine_private": list(self.machine_private),
             "pointer_pairs": [list(p) for p in self.pointer_pairs],
             "notes": list(self.notes),
             "refusals": list(self.refusals)}
        if self.stack is not None:
            d["stack"] = {"ret_kind": self.stack.ret_kind,
                          "ret_pop": self.stack.ret_pop,
                          "framed": self.stack.framed,
                          "arg_slots": [list(s) for s in self.stack.arg_slots],
                          "arg_bytes": self.stack.arg_bytes}
        return d


def _param_kinds(scan, inputs: frozenset, pairs: dict) -> list[dict]:
    """Per-input evidence: EA-address uses vs other (value) uses, and the
    kind that evidence supports ("segment" / "pointer" / "value" /
    "mixed")."""
    ea_uses: dict[str, int] = {}
    other: dict[str, int] = {}
    paired = {reg for (_, reg) in pairs} | {seg for (seg, _) in pairs}
    for i in scan.insts.values():
        in_ea: set[str] = set()
        if i.modrm is not None and i.mod != 3 \
                and not (i.mod == 0 and i.rm == 6):
            in_ea = set(_EA_REGS[i.rm]) | {_ea_segment(i)}
        elif i.modrm is not None and i.mod != 3:
            in_ea = {_ea_segment(i)}                  # direct addr: seg only
        if i.op in _STRING_PAIRS:
            for seg, reg in _STRING_PAIRS[i.op]:
                in_ea |= {seg if seg == "es" else
                          (_ea_segment(i) if i.prefixes else "ds"), reg}
        if i.op == 0xD7:
            in_ea |= {"bx", _ea_segment(i)}
        e = register_effects(i)
        for r in e.reads:
            if r not in inputs:
                continue
            if r in in_ea and i.op not in _SEG_DATA_OPS:
                ea_uses[r] = ea_uses.get(r, 0) + 1
            else:
                other[r] = other.get(r, 0) + 1
    out = []
    for r in sorted(inputs):
        if r in SEGS:
            kind = "segment"
        elif r in paired and not other.get(r):
            kind = "pointer"
        elif r in paired:
            kind = "mixed"
        else:
            kind = "value"
        out.append({"reg": r, "kind": kind,
                    "ea_sites": ea_uses.get(r, 0),
                    "other_sites": other.get(r, 0)})
    return out


def infer_contracts(ir: dict, *, external: frozenset = frozenset(),
                    names: dict[str, str] | None = None,
                    boundary_addrs: frozenset = frozenset(),
                    dispatch_addrs: frozenset = frozenset()) -> dict:
    """The M3b contract census over a recovery IR.

    ``external``: "CS:IP" keys reachable outside the static direct-call
    graph (roots, dynamic-dispatch targets, vectored handlers) -- their
    return sets stay conservative.  ``boundary_addrs`` / ``dispatch_addrs``:
    (cs, ip) sets of boundary heads and dynamic-arrival addresses.  A
    function CONTAINING one widens its inputs to the full bundle (mirroring
    the mechanical emitter); a park site observes the full bundle in the
    narrowing liveness; an alt-entry function's returns stay conservative
    (its outputs also exit through the dispatcher).  ``names``: optional
    recovered-name metadata; provenance stays the address, names attach as
    ``name [CS:IP]``.

    Returns a deterministic JSON-ready census: per-function proposals plus
    the summary work list.  A function with any refusal is NOT
    contract-promotable -- the refusal names the evidence."""
    names = names or {}
    scans, skipped = build_scans(ir)
    observers: dict[str, frozenset] = {}
    alt_entry_keys: set[str] = set()
    for key, scan in scans.items():
        cs = int(key.split(":")[0], 16)
        heads = frozenset(ip for (hcs, ip) in boundary_addrs
                          if hcs == cs and ip in scan.insts)
        if heads:
            observers[key] = heads
        if any(hcs == cs and ip in scan.insts and ip != scan.entry
               for (hcs, ip) in dispatch_addrs):
            alt_entry_keys.add(key)
    widen_full = frozenset(observers) | frozenset(alt_entry_keys)
    effects, reports, notes = compose_effects(ir, scans,
                                              widen_full=widen_full)
    observed, obs_status = observed_returns(
        ir, scans, effects,
        external=external | frozenset(alt_entry_keys),
        observers=observers)

    proposals: dict[str, ContractProposal] = {}
    for key in sorted(ir["functions"]):
        klist = list(notes.get(key, []))
        nm = names.get(key)
        if nm:
            klist.append(f"name: {nm} [{key}]")
        if key in skipped:
            proposals[key] = ContractProposal(
                key=key, notes=tuple(klist),
                refusals=({"reason": skipped[key],
                           "evidence": "recovery IR record"},))
            continue
        scan = scans[key]
        inputs, outputs = effects[key]
        refusals: list[dict] = []
        rep = reports.get(key)
        if rep is not None:
            for cap, ips in sorted(rep.refusals.items()):
                if cap == "call-abi-composition":
                    continue                          # composed above
                refusals.append({"reason": f"abi:{cap}",
                                 "evidence": [f"{ip:04X}" for ip in ips]})
        if "conservative-composition" in klist:
            refusals.append({"reason": "unresolved-call-composition",
                             "evidence": "cycle or unresolved direct callee"})

        stack = stack_args_of(scan)
        for r in stack.refusals:
            refusals.append({"reason": f"stack:{r}",
                             "evidence": f"ret_kind={stack.ret_kind} "
                                         f"ret_pop={stack.ret_pop} "
                                         f"slots={list(stack.arg_slots)}"})
        pairs = pointer_pairs_of(scan, inputs)
        # cs is the function's own static code segment -- a per-function
        # constant (push cs / cs-relative reads), never a runtime parameter:
        # the emitter materialises it as a local.  ss is machine UNLESS this
        # function uses it purely as a data-segment selector (see
        # ss_is_data_segment), in which case it is an ordinary segment input.
        machine_regs = (frozenset({"sp"}) if ss_is_data_segment(scan)
                        else MACHINE_REGS)
        machinery = sorted((inputs & (machine_regs | frozenset({"cs"})))
                           | ({"bp"} if stack.framed and "bp" in inputs
                              else frozenset()))
        semantic_in = inputs - machine_regs - frozenset({"cs"}) - (
            frozenset({"bp"}) if stack.framed else frozenset())
        params = _param_kinds(scan, semantic_in, pairs)
        rets = sorted(observed[key] - MACHINE_REGS)
        dropped = sorted((outputs - MACHINE_REGS) - set(rets))
        proposals[key] = ContractProposal(
            key=key,
            params=tuple(params), stack=stack,
            returns=tuple(rets),
            returns_status=obs_status[key],
            dropped_outputs=tuple(dropped),
            machine_private=tuple(machinery),
            pointer_pairs=tuple((s, r, n) for (s, r), n
                                in sorted(pairs.items())),
            notes=tuple(klist),
            refusals=tuple(refusals))

    promotable = sorted(k for k, p in proposals.items() if not p.refusals)
    refusal_counts: dict[str, int] = {}
    for p in proposals.values():
        for r in p.refusals:
            refusal_counts[r["reason"]] = refusal_counts.get(r["reason"], 0) + 1
    narrowed = [k for k, p in proposals.items()
                if p.returns_status == "narrowed"]
    return {
        "_notice": "GENERATED by dos_re.lift.contracts.infer_contracts -- "
                   "the M3b ABI-contract census. Regenerate, do not "
                   "hand-edit.",
        "summary": {
            "total": len(proposals),
            "contract_promotable": len(promotable),
            "returns_narrowed": len(narrowed),
            "dropped_output_total": sum(len(p.dropped_outputs)
                                        for p in proposals.values()),
            "refusal_counts": dict(sorted(refusal_counts.items(),
                                          key=lambda kv: (-kv[1], kv[0]))),
        },
        "promotable": promotable,
        "functions": {k: p.as_json() for k, p in sorted(proposals.items())},
    }
