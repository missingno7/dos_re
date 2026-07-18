"""Seeded differential: mechanical CPUless core vs DE-STACKED ABI core.

The M3b slice-2 verifier (docs/dos_re_2.0.md Stage 2b): for one function,
drive the mechanical recovered implementation and the ABI-recovered core
over the SAME deterministic pseudo-random machine states and require:

  * every OBSERVED (contract) return value equal;
  * the compat channel (exit flags word, fmask, virtual-time cost) equal --
    callers merge flags through it and the PIT reads the cost;
  * every SEMANTIC memory write equal, in order -- the mechanical side's
    machine-stack traffic lives in a shadow overlay (see TraceMem), because
    the ABI core keeps exactly those locals in the virtual stack ON
    PURPOSE, and the shadow models the program's no-alias precondition;
  * or, when a state drives a path that raises (a runtime-dead exit stub, a
    guest fault surfaced by the translator), BOTH sides raise equally.

The states are seeded, not recorded: a destackable LEAF function's behavior
is a pure function of (memory contents, register inputs), so a
deterministic synthetic state exercises it exactly; determinism keeps every
run reproducible (the automation principle).  The end-to-end demo remains
the acceptance authority for the composed graph.
"""
from __future__ import annotations

#: the stack segment handed to the MECHANICAL side; its writes there are
#: the virtualised residue.  Register seeds deliberately never collide with
#: it, so filtering by segment never hides a semantic write.
STACK_SEG = 0x7000
STACK_SP = 0x1000


class TraceMem:
    """Deterministic pseudo-random flat guest memory with a write trace.

    Reads of never-written bytes derive from (linear address, seed) -- the
    same value on both sides; writes overlay and are recorded as
    ``(seg, off, value, width)`` in order.

    ``shadow_stack_seg`` (the mechanical side): accesses through that
    segment -- which the destack gate proves are ONLY push/pop/machine
    traffic -- live in a SEPARATE overlay and are excluded from the write
    trace.  This models the real program's no-alias precondition exactly
    (its stack region is dedicated; ds/es never point into the live
    stack), so a seeded semantic pointer cannot manufacture stack aliasing
    the game never exhibits -- the same assumption the virtual stack
    makes."""

    def __init__(self, seed: int, shadow_stack_seg: int | None = None) -> None:
        self.seed = seed & 0xFFFFFFFF
        self.data: dict[int, int] = {}
        self.shadow: dict[int, int] = {}
        self.shadow_seg = shadow_stack_seg
        self.writes: list[tuple[int, int, int, int]] = []

    def _byte(self, lin: int, store: dict) -> int:
        lin &= 0xFFFFF
        v = store.get(lin)
        if v is not None:
            return v
        h = (lin * 2654435761 ^ self.seed * 40503) & 0xFFFFFFFF
        return (h >> 13) & 0xFF

    def _store(self, seg: int) -> dict:
        return self.shadow if seg == self.shadow_seg else self.data

    def rb(self, seg: int, off: int) -> int:
        return self._byte((seg << 4) + (off & 0xFFFF), self._store(seg))

    def rw(self, seg: int, off: int) -> int:
        lin = (seg << 4) + (off & 0xFFFF)
        st = self._store(seg)
        return self._byte(lin, st) | (self._byte(lin + 1, st) << 8)

    def wb(self, seg: int, off: int, val: int) -> None:
        st = self._store(seg)
        lin = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
        st[lin] = val & 0xFF
        if st is self.data:
            self.writes.append((seg, off & 0xFFFF, val & 0xFF, 1))

    def ww(self, seg: int, off: int, val: int) -> None:
        st = self._store(seg)
        lin = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
        st[lin] = val & 0xFF
        st[(lin + 1) & 0xFFFFF] = (val >> 8) & 0xFF
        if st is self.data:
            self.writes.append((seg, off & 0xFFFF, val & 0xFFFF, 2))


def _seeded_regs(params, state: int) -> dict[str, int]:
    """Deterministic register inputs for one state (never STACK_SEG)."""
    out = {}
    for k, r in enumerate(sorted(params)):
        v = ((state * 48271 + k * 214013 + 2531011) >> 5) & 0xFFFF
        if v == STACK_SEG:
            v ^= 0x0101
        out[r] = v
    return out


class PlatStub:
    """A deterministic platform interface for the differential: ``inp``
    returns a stable pseudo-random word keyed by (port, width, cost) -- the
    SAME value on both sides, since both compute the same virtual time at
    the same site -- and every call is recorded, so a divergence in port,
    width, cost, or ORDER surfaces.  A cost or _base mismatch changes the
    key, so the two sides read different values and the comparison fails
    loudly -- exactly the timing bug the compat channel exists to catch."""

    def __init__(self, seed: int) -> None:
        self.seed = seed & 0xFFFFFFFF
        self.log: list = []

    def inp(self, port, width, cost):
        self.log.append(("in", port, width, cost))
        h = (port * 2246822519 ^ cost * 3266489917 ^ self.seed) & 0xFFFFFFFF
        return (h >> 11) & (0xFFFF if width == 2 else 0xFF)

    def outp(self, port, val, width, cost):
        self.log.append(("out", port, val & 0xFFFF, width, cost))

    #: registers a DOS/BIOS service returns (mirrors emit_abi._INT_REGS).
    _INT_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")

    def intr(self, n, ib, cost):
        # fold the FULL input bundle deterministically (no built-in hash --
        # string hashing is per-process randomized): a wrong reg or flags
        # word in, or a cost drift, diverges the log AND the returned values.
        items = tuple(sorted((k, v) for k, v in ib.items()))
        self.log.append(("int", n, cost, items))
        base = (n * 40503 + cost * 2654435761 + self.seed) & 0xFFFFFFFF
        for idx, (k, v) in enumerate(items):
            base = (base * 16777619 + v + idx) & 0xFFFFFFFF
        out = {}
        for k, r in enumerate(self._INT_REGS):
            out[r] = ((base * 2654435761) >> (3 + k)) & 0xFFFF
        out["flags"] = (base >> 7) & 0xED5
        return out


def _run(fn, mem, kwargs, plat=None, args=()):
    """(outcome_kind, payload): a normal result or the raised error text.

    ``args`` are POSITIONAL semantic inputs (the ABI core's anonymous
    contract); ``kwargs`` are register-named (the mechanical ABI) plus the
    private compat channel."""
    try:
        if plat is not None:
            out, compat = fn(mem, plat, *args, **kwargs)
        else:
            out, compat = fn(mem, *args, **kwargs)
        return "ok", (out, compat)
    except ZeroDivisionError:
        return "raise", "ZeroDivisionError"
    except RuntimeError as e:
        return "raise", f"RuntimeError:{str(e)[:60]}"


def diff_one(mech_fn, abi_core_fn, proposal: dict, *, states: int = 32,
             seed0: int = 1) -> dict:
    """Differential over ``states`` seeded machine states.  Returns a report
    dict; ``report['mismatches']`` empty means the ABI core IS the
    mechanical core for every driven state (observed returns + compat +
    semantic writes)."""
    params = sorted(p["reg"] for p in proposal["params"])
    returns = list(proposal["returns"])
    mech_kd = getattr(mech_fn, "__kwdefaults__", None) or {}
    abi_kd = getattr(abi_core_fn, "__kwdefaults__", None) or {}
    # a needs_plat core takes plat as its 2nd positional (mem, plat, *, ...)
    mech_pos = mech_fn.__code__.co_varnames[:mech_fn.__code__.co_argcount]
    abi_pos = abi_core_fn.__code__.co_varnames[:abi_core_fn.__code__.co_argcount]
    mech_plat = "plat" in mech_pos
    abi_plat = "plat" in abi_pos
    missing = [r for r in params if r not in mech_kd]
    if missing:
        return {"states": 0, "raised": 0, "ok": False,
                "mismatches": [f"contract param(s) {missing} not accepted "
                               f"by the mechanical signature"]}
    mismatches: list[str] = []
    raises = 0
    for s in range(states):
        regs = _seeded_regs(params, seed0 + s)
        mem_m = TraceMem(seed0 + s, shadow_stack_seg=STACK_SEG)
        mem_a = TraceMem(seed0 + s)
        plat_m = PlatStub(seed0 + s) if mech_plat else None
        plat_a = PlatStub(seed0 + s) if abi_plat else None
        mkw = dict(regs)
        if "sp" in mech_kd:
            mkw["sp"] = STACK_SP
        if "ss" in mech_kd:
            mkw["ss"] = STACK_SEG
        akw = dict(regs)
        if "_df" in abi_kd:
            dfv = (seed0 + s) & 1
            akw["_df"] = dfv
            if "_df" in mech_kd:
                mkw["_df"] = dfv
        if "_flags_in" in abi_kd:
            # a flags-livein core takes the caller's full FLAGS word; seed a
            # varied one (with the reserved bit 0x2 always set, as the CPU
            # keeps it) so IF/DF and the arithmetic bits are exercised.
            fw = (((seed0 + s) * 2654435761) & 0xED5) | 0x2
            akw["_flags_in"] = fw
            if "_flags_in" in mech_kd:
                mkw["_flags_in"] = fw
        # the ABI core takes its semantic inputs POSITIONALLY in contract
        # order; only the private compat channel stays keyword.
        apos = tuple(regs[r] for r in params)
        akw = {k: v for k, v in akw.items() if k.startswith("_")}
        mk, mp = _run(mech_fn, mem_m, mkw, plat_m)
        ak, ap = _run(abi_core_fn, mem_a, akw, plat_a, apos)
        if mk == "raise" or ak == "raise":
            raises += 1
            if (mk, mp) != (ak, ap):
                mismatches.append(f"state {s}: raise mismatch "
                                  f"mech={mk}:{mp} abi={ak}:{ap}")
            elif raises >= 3 and raises == s + 1 and not mismatches:
                # a spin-wait function: every state so far raised the spin
                # cap identically on both sides.  Further seeded states
                # cannot exercise it (static memory never changes the
                # awaited flag) -- stop early and report the limitation.
                return {"states": s + 1, "raised": raises, "mismatches": [],
                        "ok": True,
                        "note": "spin-wait: all driven states hit the "
                                "iteration cap identically on both sides"}
            continue
        mo, mc = mp
        ao, ac = ap
        # mechanical returns a register-keyed dict; the ABI core returns a
        # POSITIONAL tuple in contract order -- compare role by role.
        for n, r in enumerate(returns):
            av = ao[n] if n < len(ao) else None
            if mo.get(r) != av:
                mismatches.append(f"state {s}: return #{n} ({r}) "
                                  f"mech={mo.get(r)!r} abi={av!r}")
        if mc != ac:
            mismatches.append(f"state {s}: compat mech={mc} abi={ac}")
        if mem_m.writes != mem_a.writes:
            mismatches.append(
                f"state {s}: writes mech(sem)={mem_m.writes[:6]}... "
                f"abi={mem_a.writes[:6]}...")
        log_m = plat_m.log if plat_m else []
        log_a = plat_a.log if plat_a else []
        if log_m != log_a:
            mismatches.append(
                f"state {s}: plat calls mech={log_m[:6]}... "
                f"abi={log_a[:6]}...")
        if len(mismatches) > 8:
            break
    return {"states": states, "raised": raises,
            "mismatches": mismatches, "ok": not mismatches}
