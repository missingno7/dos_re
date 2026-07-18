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

import hashlib

#: the stack segment handed to the MECHANICAL side; its writes there are
#: the virtualised residue.  Register seeds deliberately never collide with
#: it, so filtering by segment never hides a semantic write.
STACK_SEG = 0x7000
STACK_SP = 0x1000


#: THE verdict lattice.  One typed model, consumed by every consumer --
#: aggregation, cache membership, printed summary, CLI exit status.
#:
#: Proof status used to be represented independently in SEVEN places
#: (mismatches, ok, status, notes, cache membership, summary text, exit code)
#: and those representations repeatedly disagreed: that disagreement, not any
#: single bug, is what produced every false green in this component.  Nothing
#: may re-derive "green" from an empty mismatch list or a boolean; there is
#: one verdict and everything reads it.
#:
#:   INTERNAL_ERROR  the verifier itself failed -- proves nothing
#:   MISMATCH        outcomes or effects differ
#:   INCONCLUSIVE    some state reached a spin/unsupported frontier, so that
#:                   input established nothing
#:   VERIFIED        every state compared returns, compat and effects
#:
#: Ordered WORST-FIRST: aggregating a set of verdicts takes the minimum, so a
#: single inconclusive state cannot be outvoted by conclusive ones.
INTERNAL_ERROR, MISMATCH, INCONCLUSIVE, VERIFIED = range(4)

_VERDICT_NAME = {INTERNAL_ERROR: "internal-error", MISMATCH: "mismatch",
                 INCONCLUSIVE: "inconclusive", VERIFIED: "verified"}

#: process exit status per verdict -- distinct, so a shell chain or CI can
#: tell "not proven" from "proven wrong" instead of collapsing both to 1.
VERDICT_EXIT = {VERIFIED: 0, MISMATCH: 1, INCONCLUSIVE: 2, INTERNAL_ERROR: 3}


def verdict_name(v: int) -> str:
    return _VERDICT_NAME[v]


def aggregate(verdicts) -> int:
    """UNIVERSAL aggregation: the worst state decides.

    Positive evidence for one input does not resolve another input that
    established nothing -- one normal match plus 63 matching unsupported
    faults is not a verified function.  An earlier existential rule ("any
    normal state => verified") said otherwise, and a test defended it.
    """
    vs = list(verdicts)
    return min(vs) if vs else INCONCLUSIVE


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

    #: Hard cap on the RETAINED write trace.  The trace exists to compare
    #: two runs, which a streaming digest does at O(1) memory -- the list is
    #: only for readable diagnostics.  Without a cap a single pathological
    #: function eats the machine: 1010:37C2 spins to the 20M-iteration cap
    #: and, whenever its stack traffic is traced rather than shadowed,
    #: appends ~20,000,000 tuples (~1.4 GB for ONE list, and a comparison
    #: builds several).  A 50 GB process is a worse failure than a loud one.
    MAX_TRACE = 200_000

    def __init__(self, seed: int, shadow_stack_seg: int | None = None,
                 ss_seg: int | None = None,
                 ss_globals_floor: int | None = None) -> None:
        self.seed = seed & 0xFFFFFFFF
        self.data: dict[int, int] = {}
        self.shadow: dict[int, int] = {}
        self.shadow_seg = shadow_stack_seg
        # slice 9: ss is a SEMANTIC selector for globals in a function that
        # ALSO uses the machine stack.  The shadow overlay is off (the globals
        # must be compared), so the mechanical side's push/pop through this
        # same segment would show up as semantic writes the de-stacked side
        # never makes -- a guaranteed false divergence.  Writes at or above
        # the floor are the machine stack; below it are the globals.  Same
        # split as STACK_DATA_FLOOR in scripts/acceptance_cpuless.py.
        self.ss_seg = ss_seg
        self.ss_globals_floor = ss_globals_floor
        self.writes: list[tuple[int, int, int, int]] = []
        #: order-sensitive rolling digest of EVERY traced write, kept even
        #: after the retained list is capped -- so a divergence past the cap
        #: still fails the comparison instead of being silently equal.
        self._wh = hashlib.sha256()
        self.write_count = 0
        self.truncated = False

    @property
    def write_digest(self) -> str:
        return self._wh.hexdigest()

    def _is_machine_stack(self, seg: int, off: int) -> bool:
        return (self.ss_globals_floor is not None
                and seg == self.ss_seg
                and off >= self.ss_globals_floor)

    def _store_at(self, seg: int, off: int) -> dict:
        """The backing store for one ACCESS -- by (seg, off), not by seg.

        An ss-globals function needs its segment split: offsets below the
        floor are real globals and must live in ``data`` so both sides
        compare them, while offsets at or above it are machine stack and must
        live in the SHADOW.  Excluding them from the write TRACE (which is all
        the first version did) is not enough -- they were still written into
        the shared image, where the synthetic stack aliases a seeded pointer:
        with ds=0x6BAF and ss=0x7000, ds:0x5510 and ss:0x1000 are the same
        linear byte, so the mechanical side's pushes corrupted ds-visible data
        that the de-stacked side never touches.  Three cores diverged at state
        58 for exactly that reason.

        The shadow is what models the program's real no-alias precondition
        (its stack region is dedicated; ds/es never point into the live
        stack).  Dropping it for these functions manufactured aliasing the
        game never exhibits.
        """
        if seg == self.shadow_seg or self._is_machine_stack(seg, off):
            return self.shadow
        return self.data

    def _record(self, w) -> None:
        self.write_count += 1
        # SHA-256 over a LENGTH-DELIMITED serialization.  A 48-bit polynomial
        # was cheap telemetry, but the gate's claim is an EXACT differential
        # and two different streams can collide in 48 bits.  Delimiting also
        # removes the ambiguity a plain concatenation has, where different
        # field splits produce the same byte string.
        self._wh.update(b"|".join(b"%d" % v for v in w) + b";")
        if len(self.writes) < self.MAX_TRACE:
            self.writes.append(w)
        else:
            self.truncated = True

    def _byte(self, lin: int, store: dict) -> int:
        lin &= 0xFFFFF
        v = store.get(lin)
        if v is not None:
            return v
        h = (lin * 2654435761 ^ self.seed * 40503) & 0xFFFFFFFF
        return (h >> 13) & 0xFF

    def rb(self, seg: int, off: int) -> int:
        return self._byte((seg << 4) + (off & 0xFFFF),
                          self._store_at(seg, off & 0xFFFF))

    def rw(self, seg: int, off: int) -> int:
        lin = (seg << 4) + (off & 0xFFFF)
        st = self._store_at(seg, off & 0xFFFF)
        return self._byte(lin, st) | (self._byte(lin + 1, st) << 8)

    def wb(self, seg: int, off: int, val: int) -> None:
        st = self._store_at(seg, off & 0xFFFF)
        lin = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
        st[lin] = val & 0xFF
        if st is self.data:
            self._record((seg, off & 0xFFFF, val & 0xFF, 1))

    def ww(self, seg: int, off: int, val: int) -> None:
        st = self._store_at(seg, off & 0xFFFF)
        lin = ((seg << 4) + (off & 0xFFFF)) & 0xFFFFF
        st[lin] = val & 0xFF
        st[(lin + 1) & 0xFFFFF] = (val >> 8) & 0xFF
        if st is self.data:
            self._record((seg, off & 0xFFFF, val & 0xFFFF, 2))


def _seeded_regs(params, state: int) -> dict[str, int]:
    """Deterministic register inputs for one state (never STACK_SEG)."""
    out = {}
    for k, r in enumerate(sorted(params)):
        v = ((state * 48271 + k * 214013 + 2531011) >> 5) & 0xFFFF
        if v == STACK_SEG:
            v ^= 0x0101
        out[r] = v
    return out


def _ser(v) -> bytes:
    """Length-delimited, deterministic serialization of a logged value.

    Unambiguous by construction: every value carries a TYPE TAG and its own
    length, and unsupported types refuse rather than falling back on
    str().  (An earlier version shared one tag between tuple and list, so
    _ser((1,)) == _ser([1]) and the claim below was false.)  Feeds an incremental
    SHA-256 rather than a 48-bit polynomial, because the gate claims an EXACT
    differential and a short non-cryptographic digest can collide.
    """
    if isinstance(v, bool):
        return b"b1" if v else b"b0"
    if isinstance(v, int):
        body = b"%d" % v
        return b"i%d:%s" % (len(body), body)
    if isinstance(v, tuple):
        parts = b"".join(_ser(w) for w in v)
        return b"t%d:%s" % (len(parts), parts)
    if isinstance(v, list):
        # a DISTINCT tag from tuple: sharing one made _ser((1,)) == _ser([1]),
        # so the docstring's uniqueness claim was false as written
        parts = b"".join(_ser(w) for w in v)
        return b"l%d:%s" % (len(parts), parts)
    if isinstance(v, dict):
        parts = b"".join(_ser(k) + _ser(v[k]) for k in sorted(v))
        return b"d%d:%s" % (len(parts), parts)
    if isinstance(v, str):
        body = v.encode("utf-8", "replace")
        return b"s%d:%s" % (len(body), body)
    if v is None:
        return b"n"
    # REFUSE rather than fall back on str(): two unrelated objects with the
    # same repr would otherwise serialize identically, quietly weakening the
    # digest this gate depends on.
    raise TypeError(f"_ser: unsupported value type {type(v).__name__!r} in a "
                    f"platform log entry; add an explicit encoding for it")



class PlatStub:
    """A deterministic platform interface for the differential: ``inp``
    returns a stable pseudo-random word keyed by (port, width, cost) -- the
    SAME value on both sides, since both compute the same virtual time at
    the same site -- and every call is recorded, so a divergence in port,
    width, cost, or ORDER surfaces.  A cost or _base mismatch changes the
    key, so the two sides read different values and the comparison fails
    loudly -- exactly the timing bug the compat channel exists to catch."""

    #: Hard cap on the RETAINED call log, for exactly the reason TraceMem has
    #: one.  A spin-wait that polls a port runs to the emitter's 20,000,000
    #: iteration cap and appended a tuple EVERY time: ~1 GB per worker,
    #: measured at 962 MB and 1028 MB in two parallel workers while the run
    #: made no progress.  Capping the list while keeping an order-sensitive
    #: digest means a divergence past the cap still FAILS -- the list is only
    #: for readable diagnostics.
    #:
    #: This is the same defect as the 50 GB TraceMem.writes leak fixed earlier;
    #: that fix capped one class and left its sibling unbounded.
    MAX_LOG = 200_000

    def __init__(self, seed: int) -> None:
        self.seed = seed & 0xFFFFFFFF
        self.log: list = []
        self._lh = hashlib.sha256()
        self.log_count = 0
        self.truncated = False

    @property
    def log_digest(self) -> str:
        return self._lh.hexdigest()

    def _rec(self, item) -> None:
        self.log_count += 1
        self._lh.update(_ser(item) + b";")
        if len(self.log) < self.MAX_LOG:
            self.log.append(item)
        else:
            self.truncated = True

    def inp(self, port, width, cost):
        self._rec(("in", port, width, cost))
        h = (port * 2246822519 ^ cost * 3266489917 ^ self.seed) & 0xFFFFFFFF
        return (h >> 11) & (0xFFFF if width == 2 else 0xFF)

    def outp(self, port, val, width, cost):
        self._rec(("out", port, val & 0xFFFF, width, cost))

    #: registers a DOS/BIOS service returns (mirrors emit_abi._INT_REGS).
    _INT_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")

    def intr(self, n, ib, cost):
        # fold the FULL input bundle deterministically (no built-in hash --
        # string hashing is per-process randomized): a wrong reg or flags
        # word in, or a cost drift, diverges the log AND the returned values.
        items = tuple(sorted((k, v) for k, v in ib.items()))
        self._rec(("int", n, cost, items))
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


#: The emitters raise this exact wording when the dispatch loop exceeds
#: _ITER_CAP (see emit_cpuless/emit_abi).  Matching on it is a CONTRACT
#: between emitter and verifier: any other RuntimeError is a real fault and
#: must never be mistaken for a spin-wait.
_SPIN_MARKER = "CPUless dispatch spin"


def _is_spin_raise(payload) -> bool:
    return isinstance(payload, str) and _SPIN_MARKER in payload


def _cmp_effects(s, mem_m, mem_a, plat_m, plat_a, mismatches) -> None:
    """Compare the OBSERVABLE EFFECTS of one state: semantic memory writes and
    platform calls.

    Shared by the normal-return path and the RAISED path.  A raised state has
    no return or compat values to compare, but everything it did before
    raising is real behaviour and must still agree -- skipping it let a core
    that wrote 0xAA and one that wrote nothing compare equal so long as both
    raised the same error.  Keeping the comparison in ONE function is what
    stops the two paths drifting apart again.

    The streaming DIGEST + count is the authority; the retained lists only
    make the message readable, and comparing lists alone would silently pass a
    divergence past the retained-trace cap.
    """
    if (mem_m.write_digest, mem_m.write_count) != \
            (mem_a.write_digest, mem_a.write_count):
        mismatches.append(
            f"state {s}: writes differ (mech {mem_m.write_count} vs "
            f"abi {mem_a.write_count}) mech(sem)={mem_m.writes[:6]}... "
            f"abi={mem_a.writes[:6]}..."
            + ("  [trace truncated -- digest is the authority]"
               if mem_m.truncated or mem_a.truncated else ""))
    sig_m = ((plat_m.log_digest, plat_m.log_count) if plat_m else (0, 0))
    sig_a = ((plat_a.log_digest, plat_a.log_count) if plat_a else (0, 0))
    if sig_m != sig_a:
        log_m = plat_m.log if plat_m else []
        log_a = plat_a.log if plat_a else []
        mismatches.append(
            f"state {s}: plat calls differ "
            f"(mech {sig_m[1]} calls vs abi {sig_a[1]}) "
            f"mech={log_m[:6]}... abi={log_a[:6]}...")


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
    spin_states = 0
    #: states where BOTH sides returned normally, so returns and the compat
    #: channel were actually compared.  A core with none of these has no
    #: positive evidence at all -- see the tri-state below.
    normal_states = 0
    #: one verdict PER STATE; the core's verdict is their aggregate
    state_verdicts: list[int] = []
    # When ss is a SEMANTIC parameter (a data-segment selector), the
    # mechanical side's ss traffic is real program data, not stack residue:
    # seed ss identically on both sides and DISABLE the shadow overlay, so
    # those writes are compared instead of being hidden.
    #
    # Slice 7 (ss_is_data_segment) has no stack traffic at all, so the overlay
    # had nothing legitimate left to hide.  Slice 9 (ss_globals_floor) breaks
    # that premise: those functions reach globals through ss AND use the
    # machine stack.  With the overlay off, the mechanical side's push/pop
    # through the same segment becomes visible semantic writes the de-stacked
    # side never makes -- 30 cores failed with a constant ~167-write surplus
    # at high offsets before this was modelled.  Split by the same floor the
    # emitter uses: below it globals (compared), at/above it machine stack
    # (ignored, on BOTH sides).
    ss_semantic = "ss" in params
    ss_floor = proposal.get("ss_globals_floor")
    for s in range(states):
        regs = _seeded_regs(params, seed0 + s)
        # An ss-globals function needs a KNOWN ss on both sides so the floor
        # split has a segment to apply to; the seeded value is arbitrary.
        if ss_floor is not None and "ss" in regs:
            regs["ss"] = STACK_SEG
        ss_val = regs.get("ss")
        mem_m = TraceMem(seed0 + s,
                         shadow_stack_seg=None if ss_semantic else STACK_SEG,
                         ss_seg=ss_val, ss_globals_floor=ss_floor)
        mem_a = TraceMem(seed0 + s,
                         ss_seg=ss_val, ss_globals_floor=ss_floor)
        plat_m = PlatStub(seed0 + s) if mech_plat else None
        plat_a = PlatStub(seed0 + s) if abi_plat else None
        mkw = dict(regs)
        if "sp" in mech_kd:
            mkw["sp"] = STACK_SP
        if "ss" in mech_kd and not ss_semantic:
            mkw["ss"] = STACK_SEG          # machine stack: goes to the shadow
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
            elif _is_spin_raise(mp):
                # A spin-wait state proves nothing about the OTHER states, so
                # it is noted and skipped -- never a reason to stop.
                #
                # This used to return ok=True after three matching raises, on
                # the reasoning that "static memory never changes the awaited
                # flag".  That reasoning holds only for a genuine spin cap,
                # and the check never tested which exception it was: ANY
                # RuntimeError agreeing on states 0-2 ended a requested
                # 64-state run as PASSED.  State 3 could return normally with
                # different outputs and never be driven -- a false green, the
                # exact failure class this harness exists to prevent.
                #
                # It was a performance shortcut, and --iter-cap has since made
                # it unnecessary: every requested state now runs, at a cap
                # cheap enough to afford.
                spin_states += 1
            # FALL THROUGH to the effect comparison.  Only the RETURN and
            # compat values are unavailable when a side raises; every memory
            # write and platform call made BEFORE the exception is real
            # observable behaviour and must still agree.  `continue` here let
            # a core that wrote 0xAA and one that wrote nothing compare equal
            # as long as both raised the same spin error -- a false green with
            # the divergence sitting in plain view.
            before = len(mismatches)
            _cmp_effects(s, mem_m, mem_a, plat_m, plat_a, mismatches)
            # a raised state establishes NOTHING about returns or compat,
            # even when the effects agree: it is inconclusive, not verified
            state_verdicts.append(MISMATCH if len(mismatches) > before
                                  else INCONCLUSIVE)
            continue
        normal_states += 1
        _before = len(mismatches)
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
        _cmp_effects(s, mem_m, mem_a, plat_m, plat_a, mismatches)
        state_verdicts.append(MISMATCH if len(mismatches) > _before
                              else VERIFIED)
        if len(mismatches) > 8:
            break
    # ONE verdict, aggregated universally: the worst state decides.  `ok`
    # is DERIVED from it and means exactly "verified" -- it used to be
    # `not mismatches`, so an inconclusive core still handed every caller of
    # the compatibility field the original false green.
    verdict = aggregate(state_verdicts)
    status = verdict_name(verdict)
    rep = {"states": states, "raised": raises, "normal_states": normal_states,
           "spin_states": spin_states, "mismatches": mismatches,
           "verdict": verdict, "status": status,
           "exit_code": VERDICT_EXIT[verdict],
           "ok": verdict == VERIFIED}
    if verdict == INCONCLUSIVE:
        rep["note"] = (
            f"INCONCLUSIVE: {normal_states}/{states} states compared fully; "
            f"{states - normal_states} raised ({spin_states} at the spin cap) "
            f"and established nothing.  Partial evidence is NOT equivalence.")
    elif spin_states:
        rep["note"] = f"spin-wait: {spin_states}/{states} states hit the cap"
    return rep
