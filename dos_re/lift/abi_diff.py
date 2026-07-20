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
from dataclasses import dataclass
from enum import IntEnum

#: the stack segment handed to the MECHANICAL side; its writes there are
#: the virtualised residue.  Register seeds deliberately never collide with
#: it, so filtering by segment never hides a semantic write.
STACK_SEG = 0x7000
STACK_SP = 0x1000


#: THE verdict lattice, and the ONE result type that carries it.
#:
#: Proof status used to live in seven independent representations
#: (mismatches, ok, status, notes, cache membership, summary text, exit code)
#: which repeatedly disagreed -- that disagreement, not any single bug, caused
#: every false green here.  Centralising the CONSTANTS was not enough: while
#: results stayed loose dicts with separately-maintained fields, a state's
#: diagnostics and its verdict could still drift apart (they did: a
#: raise-vs-return mismatch was recorded as a diagnostic and then classified
#: INCONCLUSIVE because the snapshot was taken too late).
#:
#: So the verdict is DERIVED, once, from the diagnostics -- never stored
#: alongside them -- and ok/status/exit_code are properties, never fields.
class Verdict(IntEnum):
    """Ordered WORST-FIRST: aggregation takes the minimum, so one bad state
    cannot be outvoted by good ones."""

    MISMATCH = 0           # outcomes or effects differ -- DISPROVES equivalence
    INTERNAL_ERROR = 1     # the verifier failed -- proves nothing
    INCONCLUSIVE = 2       # a spin/unsupported frontier: established nothing
    VERIFIED = 3           # returns, compat and effects all compared and agreed

    # MISMATCH dominates INTERNAL_ERROR deliberately.  Corpus equivalence is a
    # universal claim, so aggregation is logical AND over three-valued logic:
    #   False AND Unknown = False
    # One established divergence disproves the corpus, and a tooling failure on
    # some OTHER core cannot un-disprove it.  The previous order let an
    # internal error outrank a real mismatch, so a run with both announced
    # "the verifier failed; no conclusion about the corpus" while holding
    # proof that the corpus was wrong.


INTERNAL_ERROR = Verdict.INTERNAL_ERROR
MISMATCH = Verdict.MISMATCH
INCONCLUSIVE = Verdict.INCONCLUSIVE
VERIFIED = Verdict.VERIFIED

#: process exit status per verdict -- distinct, so a shell chain or CI can
#: tell "not proven" from "proven wrong" instead of collapsing both to 1.
VERDICT_EXIT = {Verdict.VERIFIED: 0, Verdict.MISMATCH: 1,
                Verdict.INCONCLUSIVE: 2, Verdict.INTERNAL_ERROR: 3}


def verdict_name(v) -> str:
    return Verdict(v).name.lower().replace("_", "-")


def aggregate(verdicts) -> "Verdict":
    """UNIVERSAL aggregation: the worst verdict decides.

    Positive evidence for one input does not resolve another input that
    established nothing.  An EMPTY set is INCONCLUSIVE, not verified: nothing
    was compared, so nothing was proven -- an empty corpus or a typoed --only
    used to exit 0 claiming every core identical.
    """
    vs = [Verdict(v) for v in verdicts]
    return min(vs) if vs else Verdict.INCONCLUSIVE


@dataclass(frozen=True)
class VerdictReport:
    """One immutable result: a verdict plus the evidence it was derived from.

    ``ok``, ``status`` and ``exit_code`` are PROPERTIES.  Storing them as
    fields is exactly how "diagnostics say mismatch, verdict says
    inconclusive, status string says something else, exit code derived
    elsewhere" became possible.
    """

    verdict: Verdict
    diagnostics: tuple = ()
    states: int = 0
    normal_states: int = 0
    spin_states: int = 0
    raised: int = 0
    note: str = ""

    def __post_init__(self):
        """Reject contradictory reports at CONSTRUCTION.

        The type discouraged contradiction; these make it unrepresentable.
        VerdictReport(verdict=VERIFIED, diagnostics=("contradiction",)) was
        still buildable, which is the same class of drift the type was
        introduced to end.
        """
        v, d = self.verdict, self.diagnostics
        if v in (Verdict.VERIFIED, Verdict.INCONCLUSIVE) and d:
            raise ValueError(
                f"{verdict_name(v)} report carries diagnostics {d!r}: a "
                f"recorded divergence contradicts the verdict")
        if v in (Verdict.MISMATCH, Verdict.INTERNAL_ERROR) and not d:
            raise ValueError(
                f"{verdict_name(v)} report has no diagnostics: a failing "
                f"verdict must say what failed")
        if self.normal_states > self.states or self.raised > self.states:
            raise ValueError(
                f"counts exceed states: normal={self.normal_states} "
                f"raised={self.raised} states={self.states}")
        if self.spin_states > self.raised:
            raise ValueError(
                f"spin_states {self.spin_states} exceeds raised {self.raised}")
        if v is Verdict.VERIFIED and self.states and                 self.normal_states != self.states:
            raise ValueError(
                f"verified requires every state to have completed normally: "
                f"{self.normal_states}/{self.states}")

    @classmethod
    def internal_error(cls, message: str) -> "VerdictReport":
        """The verifier itself failed.  A factory so worker and sequential
        paths stop hand-rolling loose dicts at the process boundary."""
        return cls(verdict=Verdict.INTERNAL_ERROR, diagnostics=(message,))

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.VERIFIED

    @property
    def status(self) -> str:
        return verdict_name(self.verdict)

    @property
    def exit_code(self) -> int:
        return VERDICT_EXIT[self.verdict]

    #: dict-style reads so existing call sites keep working; every key is
    #: derived from the same verdict, so none of them can disagree.
    def __getitem__(self, k):
        if k == "mismatches":
            return list(self.diagnostics)
        return getattr(self, k)

    def get(self, k, default=None):
        try:
            return self[k]
        except AttributeError:
            return default


def state_verdict(diagnostics_added: bool, both_returned: bool) -> Verdict:
    """The ONLY place a state's verdict is decided.

    Derived from the two facts that matter, in one expression, so a diagnostic
    can never be recorded while the verdict says otherwise.
    """
    if diagnostics_added:
        return Verdict.MISMATCH
    return Verdict.VERIFIED if both_returned else Verdict.INCONCLUSIVE


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
        # split used by the historical CPUless acceptance proof.
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
        # INTERNAL_ERROR: the harness cannot drive this pair at all, which
        # says nothing about the core.  This used to return a bare dict with
        # no verdict, so the tool reconstructed one from the string status.
        return VerdictReport(
            verdict=INTERNAL_ERROR,
            diagnostics=(f"contract param(s) {missing} not accepted "
                         f"by the mechanical signature",))
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
        _before = len(mismatches)          # BEFORE any comparison for s
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
            _cmp_effects(s, mem_m, mem_a, plat_m, plat_a, mismatches)
            # A raised state establishes nothing about returns or compat even
            # when the effects agree -- but if one side RAISED while the other
            # RETURNED, that outcome mismatch was already recorded above.  The
            # snapshot is taken before BOTH comparisons so it cannot be
            # missed: taking it after the outcome check classified a genuine
            # raise-vs-return divergence as merely inconclusive.
            state_verdicts.append(
                state_verdict(len(mismatches) > _before, both_returned=False))
            continue
        normal_states += 1
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
        state_verdicts.append(
            state_verdict(len(mismatches) > _before, both_returned=True))
        if len(mismatches) > 8:
            break
    verdict = aggregate(state_verdicts)
    if verdict is INCONCLUSIVE:
        note = (f"INCONCLUSIVE: {normal_states}/{states} states compared "
                f"fully; {states - normal_states} raised ({spin_states} at "
                f"the spin cap) and established nothing.  Partial evidence "
                f"is NOT equivalence.")
    elif spin_states:
        note = f"spin-wait: {spin_states}/{states} states hit the cap"
    else:
        note = ""
    rep = VerdictReport(verdict=verdict, diagnostics=tuple(mismatches),
                        states=states, normal_states=normal_states,
                        spin_states=spin_states, raised=raises, note=note)
    return rep
