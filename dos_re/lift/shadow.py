"""SHADOW VERIFICATION: the rung between "diffed once" and "drives the program".

:mod:`dos_re.lift.standalone` can already make a hand-recovered body THE running
implementation of one address (``install_overrides``). What it cannot do is tell
you whether that body is *right*, and an island's recorded status is not an
answer: ``ASM_MATCHED`` means "diffed on captured cases", which is weaker than
the byte-exact standard a generated corpus already meets. Promoting on that
basis LOWERS the proof standard.

A shadow closes that gap. The GENERATED body still drives -- its outputs, flags
and virtual-time cost are returned unchanged, so program behaviour is provably
untouched -- while the candidate runs beside it on the same pre-state and every
observable is compared. Run a cold-start differential with shadows installed and
a full playthrough becomes a per-call proof of the candidate.

FOUR DESIGN DECISIONS, each paid for by a false green somewhere in this
ecosystem:

1. **The candidate is a DROP-IN, not a checker callback.** Ports previously
   supplied ``checker(mem, kw, outputs, compat)`` and were then free to compare
   whatever they liked; skyroads' 04C0 checker compared AX and nothing else --
   not the other six outputs, not flags, not fmask, not memory -- while tallying
   cost into a counter that *read* like an assertion and asserted nothing. Here
   the candidate has the generated signature and returns the generated
   ``(outputs, compat)`` pair, so THIS module does the comparing and the
   comparison is total by construction. It also means the artifact under proof
   is the same artifact that would ship.

2. **Everything is compared unless an :class:`Exemption` says otherwise, and an
   exemption without a written reason is an error.** A silent default subset is
   how a checker rots: nothing about it looks different from a real one.

3. **A shadow that was never called is :attr:`Verdict.INCONCLUSIVE`, never
   VERIFIED.** A frame-windowed aggregate reading zero is indistinguishable from
   "this never happened", and that exact ambiguity has produced wrong
   conclusions here twice. Zero calls is an absence of evidence and must report
   as one.

4. **The candidate does NOT get the platform** (:class:`_NoEffectPlat`). Memory
   can be overlaid; a device write cannot, and the generated body performs the
   real one regardless. So a candidate that delegates to a platform-touching
   callee fires the effect TWICE per call while the comparison agrees perfectly.
   Measured, not imagined: skyroads' ``1010:1B49`` reaches ``1010:03C2``, which
   drives port 0x61, on the arm a real playthrough takes. Every ``plat``
   attribute now raises, so "this address is not shadowable" surfaces as an
   error on the first call instead of as a perturbed run nobody attributes to
   the shadow.

Memory is compared as an ORDERED byte-write log. The candidate runs FIRST, on a
proxy whose reads pass through to the still-untouched machine and whose writes
land in an overlay -- so it observes the exact pre-state and cannot perturb the
run -- and the generated body then runs on a proxy that writes for real and logs
as it goes. Width is normalised to bytes, so ``ww`` versus two ``wb`` is not a
difference; order is not.

Usage::

    from dos_re.lift.shadow import Exemption, install_shadows, report, verdict

    install_shadows("game.recovered", {"1010:04C0": my_candidate},
                    exemptions={"1010:04C0": Exemption(
                        memory=True, reason="...why this is sound...")})
    ...                                   # run the differential
    assert verdict() is Verdict.VERIFIED, report()
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dos_re.lift.abi_diff import Verdict, verdict_name
from dos_re.lift.standalone import generated, install_overrides

__all__ = ["Exemption", "ShadowRecord", "Verdict", "verdict_name", "install_shadows",
           "records", "record_for", "report", "reset", "verdict"]

#: The compat channel's contract keys. ``flags``/``fmask`` are how a caller merges
#: the callee's exit flags into its own; ``cost`` is what the PIT reads. All three
#: are observable, so all three are compared -- cost especially, because an island
#: computes a VALUE and has no cycle count, which is precisely the thing a shadow
#: is meant to establish before the island may drive.
COMPAT_KEYS = ("flags", "fmask", "cost")


@dataclass(frozen=True)
class Exemption:
    """An explicitly justified hole in an otherwise total comparison.

    Exemptions exist because a few really are sound -- a genuinely callee-saved
    register that the generated body restores from the machine stack, or the
    machine-stack residue itself when the candidate keeps those locals in Python
    -- but every one of them is a place the proof does not reach. So each must be
    named and argued at the call site: ``reason`` is REQUIRED and an empty one
    raises. A checker that quietly compares less is indistinguishable from one
    that compares everything, and that is the failure mode this guards.
    """

    #: output register names NOT compared, e.g. ``{"di"}``
    outputs: frozenset = frozenset()
    #: compat keys NOT compared, e.g. ``{"cost"}``
    compat: frozenset = frozenset()
    #: skip the memory-write comparison entirely
    memory: bool = False
    #: why each of the above is sound. REQUIRED whenever anything is exempted.
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(self, "outputs", frozenset(self.outputs))
        object.__setattr__(self, "compat", frozenset(self.compat))
        bad = self.compat - set(COMPAT_KEYS)
        if bad:
            raise ValueError(f"unknown compat key(s) exempted: {sorted(bad)}; "
                             f"the contract is {COMPAT_KEYS}")
        if (self.outputs or self.compat or self.memory) and not self.reason.strip():
            raise ValueError(
                "an Exemption must carry a written reason -- an unexplained hole "
                "in a proof is the failure mode this class exists to prevent")


@dataclass
class ShadowRecord:
    """What one shadowed address established, and the verdict DERIVED from it.

    The verdict is a property, never a field, for the reason recorded in
    :mod:`dos_re.lift.abi_diff`: proof status stored beside its own diagnostics
    drifts from them, and every false green here came from that drift.
    """

    key: str
    exemption: Exemption = field(default_factory=Exemption)
    #: real calls on which every compared observable agreed
    calls: int = 0
    #: human-readable disagreements -- each one DISPROVES the candidate
    mismatches: list = field(default_factory=list)
    #: the shadow machinery itself failed; proves nothing either way
    errors: list = field(default_factory=list)
    #: observed virtual-time costs, so a static cost can be told from a spread
    costs: dict = field(default_factory=dict)

    @property
    def verdict(self) -> Verdict:
        if self.mismatches:
            return Verdict.MISMATCH
        if self.errors:
            return Verdict.INTERNAL_ERROR
        if self.calls == 0:
            # NOT a pass. "Never ran" and "ran and agreed" are different claims,
            # and reporting the first as the second is the exact false green this
            # ecosystem has produced twice.
            return Verdict.INCONCLUSIVE
        return Verdict.VERIFIED

    def summary(self) -> str:
        v = verdict_name(self.verdict)
        if self.mismatches:
            return f"{self.key}: {v} -- {self.mismatches[0]}"
        if self.errors:
            return f"{self.key}: {v} -- {self.errors[0]}"
        if not self.calls:
            return f"{self.key}: {v} -- NEVER CALLED (wrong replay for this address?)"
        cost = ""
        if self.costs:
            top = sorted(self.costs.items(), key=lambda kv: -kv[1])[:4]
            cost = ("  cost=STATIC " + str(top[0][0]) if len(self.costs) == 1
                    else f"  cost={len(self.costs)} distinct {dict(top)}")
        holes = ""
        if self.exemption.reason:
            e = self.exemption
            what = sorted(e.outputs | e.compat) + (["memory"] if e.memory else [])
            holes = f"  [exempt: {','.join(what)}]"
        return f"{self.key}: {v} on {self.calls:,} calls{cost}{holes}"


#: key -> ShadowRecord for every shadow installed in this process.
_RECORDS: "dict[str, ShadowRecord]" = {}


class _OverlayMem:
    """Reads see the machine; writes go to an overlay and are LOGGED.

    The candidate must observe the pre-state exactly and must not perturb the run
    -- the generated body is what drives -- but it also has to read its own
    writes back (a body that pushes and later pops would otherwise read stale
    words). So the overlay is consulted first on every read.
    """

    __slots__ = ("_mem", "_over", "log")

    def __init__(self, mem):
        self._mem = mem
        self._over: "dict[int, int]" = {}
        self.log: "list[tuple[int, int]]" = []

    def _rb(self, lin: int) -> int:
        v = self._over.get(lin)
        return self._mem.rb(lin >> 4, lin & 0xF) if v is None else v

    def _wb(self, lin: int, val: int) -> None:
        val &= 0xFF
        self._over[lin] = val
        self.log.append((lin, val))

    def rb(self, seg, off):
        return self._rb(((seg & 0xFFFF) << 4) + (off & 0xFFFF))

    def rw(self, seg, off):
        base = ((seg & 0xFFFF) << 4)
        off &= 0xFFFF
        return self._rb(base + off) | (self._rb(base + ((off + 1) & 0xFFFF)) << 8)

    def wb(self, seg, off, val):
        self._wb(((seg & 0xFFFF) << 4) + (off & 0xFFFF), val)

    def ww(self, seg, off, val):
        base = ((seg & 0xFFFF) << 4)
        off &= 0xFFFF
        val &= 0xFFFF
        self._wb(base + off, val & 0xFF)
        self._wb(base + ((off + 1) & 0xFFFF), val >> 8)

    def __getattr__(self, name):                     # anything else, verbatim
        return getattr(self._mem, name)


class _RecordMem:
    """Writes through to the real machine AND logs them, byte-normalised."""

    __slots__ = ("_mem", "log")

    def __init__(self, mem):
        self._mem = mem
        self.log: "list[tuple[int, int]]" = []

    def rb(self, seg, off):
        return self._mem.rb(seg, off)

    def rw(self, seg, off):
        return self._mem.rw(seg, off)

    def wb(self, seg, off, val):
        self.log.append((((seg & 0xFFFF) << 4) + (off & 0xFFFF), val & 0xFF))
        self._mem.wb(seg, off, val)

    def ww(self, seg, off, val):
        base = ((seg & 0xFFFF) << 4)
        off &= 0xFFFF
        val &= 0xFFFF
        self.log.append((base + off, val & 0xFF))
        self.log.append((base + ((off + 1) & 0xFFFF), val >> 8))
        self._mem.ww(seg, off, val)

    def __getattr__(self, name):
        return getattr(self._mem, name)


class _NoEffectPlat:
    """The platform as the CANDIDATE may see it: not at all.

    Memory can be overlaid, so the candidate observes the pre-state and writes
    nowhere real. A PLATFORM EFFECT cannot be -- ``plat.outp`` reaches a device,
    not a byte -- and the generated body is going to perform the real one a
    moment later. A candidate that delegates to a platform-touching callee
    therefore fires the effect TWICE per call, silently, and the second copy is
    indistinguishable from the game doing it.

    That is not hypothetical: skyroads' ``1010:1B49`` reaches ``1010:03C2``,
    which does ``inp(0x61)``/``outp(0x61, 3)`` on the PC speaker, on the arm a
    real playthrough actually takes. Nothing in the comparison would have shown
    it -- the outputs would agree perfectly while the run had been perturbed.

    So every attribute is a refusal. An address whose body performs platform
    effects is simply not shadowable by this instrument, and finding that out is
    an ERROR at the first call rather than a difference nobody looks for.
    """

    __slots__ = ("_key",)

    def __init__(self, key: str):
        self._key = key

    def __getattr__(self, name):
        def _refuse(*_a, **_k):
            raise AssertionError(
                f"shadow {self._key}: the candidate called plat.{name}(...). A "
                f"shadowed candidate runs BESIDE the generated body, which "
                f"performs the real effect -- so this would fire it twice and "
                f"perturb the run the shadow exists to leave untouched. Memory "
                f"is overlaid; platform effects cannot be. An address whose body "
                f"performs platform I/O is not shadowable: prove it another way.")

        return _refuse


def _diff_memory(want: list, got: list) -> "str | None":
    """First divergence in the ordered byte-write logs, or None."""
    for i, (w, g) in enumerate(zip(want, got)):
        if w != g:
            return (f"memory write #{i} differs: generated wrote "
                    f"{w[1]:02X} at {w[0]:05X}, candidate wrote {g[1]:02X} at {g[0]:05X}")
    if len(want) != len(got):
        extra = (want if len(want) > len(got) else got)[min(len(want), len(got))]
        who = "generated" if len(want) > len(got) else "candidate"
        return (f"memory write COUNT differs: generated {len(want)}, candidate "
                f"{len(got)}; first unmatched is {who}'s {extra[1]:02X} at {extra[0]:05X}")
    return None


def _compare(rec: ShadowRecord, kw, want_o, want_c, want_w, got_o, got_c, got_w) -> "str | None":
    """Every observable, minus what the exemption explicitly excuses."""
    e = rec.exemption
    ctx = " ".join(f"{k}={v:04X}" if isinstance(v, int) else f"{k}={v!r}"
                   for k, v in sorted(kw.items()))

    missing = set(want_o) - set(got_o) - e.outputs
    if missing:
        return (f"candidate omits output(s) {sorted(missing)} the generated "
                f"contract returns; inputs: {ctx}")
    for name in sorted(want_o):
        if name in e.outputs:
            continue
        w, g = want_o[name] & 0xFFFF, got_o[name] & 0xFFFF
        if w != g:
            return (f"output {name} differs: generated={w:04X} candidate={g:04X}; "
                    f"inputs: {ctx}")
    for name in COMPAT_KEYS:
        if name in e.compat:
            continue
        if name not in want_c:
            continue
        if name not in got_c:
            return f"candidate declares no {name!r}; inputs: {ctx}"
        w, g = want_c[name], got_c[name]
        if w != g:
            return (f"compat {name} differs: generated={w:#x} candidate={g:#x}; "
                    f"inputs: {ctx}")
    if not e.memory:
        d = _diff_memory(want_w, got_w)
        if d is not None:
            return f"{d}; inputs: {ctx}"
    return None


def install_shadows(package: str, candidates: "dict", *, exemptions: "dict" = None,
                    fail_fast: bool = True) -> "list[str]":
    """Install ``{'CS:IP': candidate}`` as shadows over ``package``'s generated bodies.

    Each candidate must have the generated signature and return the generated
    ``(outputs, compat)`` pair. The generated body drives; the candidate is run
    beside it and every observable compared (see :class:`Exemption` for the only
    sanctioned way to compare less).

    With ``fail_fast`` the first disagreement RAISES, in the middle of the run,
    which is what you want from a gate: a recorded-and-continued mismatch is one
    more thing that has to be read to be noticed. It is recorded either way, so
    :func:`verdict` is authoritative regardless.
    """
    exemptions = exemptions or {}
    unknown = set(exemptions) - set(candidates)
    if unknown:
        raise ValueError(f"exemption(s) for un-shadowed address(es): {sorted(unknown)}")

    wrapped = {}
    for key, candidate in candidates.items():
        rec = _RECORDS[key] = ShadowRecord(key, exemptions.get(key, Exemption()))
        wrapped[key] = _make_shadow(package, key, candidate, rec, fail_fast)
    return install_overrides(package, wrapped)


def _plat_position(gen) -> int:
    """Index in ``*args`` of the generated body's ``plat``, or -1 if it has none.

    The corpus convention is ``func(mem, plat, *, regs...)``, but this reads the
    signature rather than assuming position: a body with no platform parameter
    must not have a phantom one substituted.
    """
    import inspect

    try:
        params = list(inspect.signature(gen).parameters.values())
    except (TypeError, ValueError):                  # builtins, C wrappers
        return -1
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    for i, p in enumerate(positional[1:]):           # [0] is mem
        if p.name == "plat":
            return i
    return -1


def _make_shadow(package: str, key: str, candidate, rec: ShadowRecord, fail_fast: bool):
    gen = generated(package, key)                    # resolved BEFORE the shadow lands
    plat_at = _plat_position(gen)

    def shadowed(mem, *args, **kw):
        # Candidate FIRST, on the untouched pre-state, writing only to an overlay
        # -- and with the PLATFORM withheld, because an effect cannot be overlaid
        # and the generated body is about to perform the real one.
        cand_mem = _OverlayMem(mem)
        cand_args, cand_kw = args, kw
        if plat_at >= 0:
            if len(args) > plat_at:
                cand_args = (args[:plat_at] + (_NoEffectPlat(key),)
                             + args[plat_at + 1:])
            elif "plat" in kw:                       # passed by name, not position
                cand_kw = dict(kw, plat=_NoEffectPlat(key))
        try:
            got_o, got_c = candidate(cand_mem, *cand_args, **cand_kw)
        except Exception as exc:                     # noqa: BLE001 -- classified, not swallowed
            rec.mismatches.append(
                f"candidate raised {type(exc).__name__}: {exc} where the generated "
                f"body returns")
            if fail_fast:
                raise AssertionError(f"shadow {key}: {rec.mismatches[-1]}") from exc
            return gen(mem, *args, **kw)

        real_mem = _RecordMem(mem)
        want_o, want_c = gen(real_mem, *args, **kw)  # THIS is what drives

        try:
            bad = _compare(rec, kw, want_o, want_c, real_mem.log,
                           got_o, got_c, cand_mem.log)
        except Exception as exc:                     # noqa: BLE001 -- the shadow itself broke
            rec.errors.append(f"{type(exc).__name__}: {exc}")
            if fail_fast:
                raise
            return want_o, want_c

        if bad is not None:
            rec.mismatches.append(bad)
            if fail_fast:
                raise AssertionError(f"shadow {key} disagrees with the generated body: {bad}")
        else:
            rec.calls += 1
            c = want_c.get("cost")
            if c is not None:
                rec.costs[c] = rec.costs.get(c, 0) + 1
        return want_o, want_c

    shadowed.__name__ = f"shadow_{key.replace(':', '_').lower()}"
    shadowed.__doc__ = f"Shadow of {key}: generated body drives, candidate compared."
    return shadowed


def records() -> "dict[str, ShadowRecord]":
    return dict(_RECORDS)


def record_for(key: str) -> ShadowRecord:
    return _RECORDS[key]


def reset() -> None:
    _RECORDS.clear()


def verdict() -> Verdict:
    """Worst verdict across every installed shadow.

    Aggregation is ``min`` over the worst-first lattice, so one bad shadow cannot
    be outvoted. With NO shadows installed the answer is INCONCLUSIVE: nothing was
    established, and that is not the same as success.
    """
    if not _RECORDS:
        return Verdict.INCONCLUSIVE
    return min(r.verdict for r in _RECORDS.values())


def report() -> str:
    """One line per shadow, prefixed by the aggregate verdict."""
    if not _RECORDS:
        return "INCONCLUSIVE -- no shadow was installed, so nothing was established"
    body = "; ".join(_RECORDS[k].summary() for k in sorted(_RECORDS))
    return f"{verdict_name(verdict()).upper()} -- {body}"
