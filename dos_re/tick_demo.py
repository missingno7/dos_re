"""Game-tick demos — the endgame's mode-independent equivalence proof (VM ⇄ VM-less native).

The legacy input demo (``input_demo``) keys input to PRESENT frames and advances the VM by a fixed
INSTRUCTION budget per frame. That clock is **mode-dependent**: a recovered hook runs far fewer emulated
instructions than the ASM it replaces, so the same demo advances the game by a different amount in
pure-ASM / hybrid / native mode — and the VM-less native core has no instruction count at all. (Proven on
the first completed port: a pure-ASM recording drifted in the hybrid while every gameplay hook verified
byte-exact — the divergence was purely the clock, never the logic.)

A **tick demo** is keyed to the GAME TICK instead — one main-loop iteration of the original game. Per tick
it stores the input the game actually *consumed* plus a digest of the gameplay state after the tick.
Replaying steps exactly one tick and injects those keys, so the same recording runs IDENTICALLY in every
mode, and the VM-less native core consumes it directly:

    record (VM = the oracle, any mode)          verify (no VM at all)
    ────────────────────────────────────        ─────────────────────────────────
    per game tick:                              per game tick:
      keys the game sampled                       inject recorded keys + sidebands
      sideband values (see below)                 run ONE native tick
      digest(gameplay state)                      assert digest matches recording

If the native core reproduces the digest at EVERY tick, the VM-less game provably reproduces the original
byte-for-byte over the whole recording. This is the proof the engine-flip rests on (the porting method's
"full demo corpus passes native-vs-VM tick-by-tick" exit condition).

Three hard-won rules are baked into this module's shape (each cost the source port a real divergence):

* **Capture at the consumption point.** Keys are captured where the tick CONSUMES them, not where the
  frame starts — an ISR delivering a make/break between the two changes the value *after* it was consumed
  and the recording then lies. The adapter places the observer(s); a later observer simply overwrites the
  pending value (the "refine" pattern: capture a safe base early, overwrite with the exact consumed value
  at the true read site).
* **Sidebands: record-and-inject what the native core cannot reproduce.** State derived from the
  instruction count or wall clock (an idle-fidget timer fed by the PIT, a frame-skew counter) has no
  VM-less equivalent. Record its per-tick value from the VM and inject it before each native tick, or the
  digest diverges on state the gameplay logic only *reads*.
* **The digest boundary is the ownership map.** Digest exactly the state the gameplay tick OWNS: exclude
  render-only state, input plumbing, and async audio — the same mask the forward lockstep oracle proves
  byte-exact, so a digest match means "same gameplay" by the same definition. :func:`masked_digest` is the
  mask-applying helper; the offsets are the adapter's (they ARE the game knowledge).

Everything game-specific — seam addresses, the key-cell list, the exclusion mask, the tick function and
its transition outcomes — is adapter-supplied. This module owns the container + on-disk format, the
step-hook recorder plumbing, and the inject → tick → compare loop.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, MutableMapping

__all__ = ["TickDemo", "masked_digest", "record_ticks", "replay_to", "verify_ticks"]


def masked_digest(region: bytes | bytearray | memoryview, *,
                  zero: Iterable[int] = (),
                  mask: Iterable[tuple[int, int]] = (),
                  post: Callable[[bytearray], None] | None = None) -> str:
    """SHA1 of ``region`` with the non-owned state neutralised — the fingerprint two runs must share if
    they computed the same gameplay tick.

    ``zero`` offsets are zeroed (state the tick does not own: render, input plumbing, async audio);
    ``mask`` is ``(offset, and_mask)`` pairs for bytes only partially owned (e.g. render page/visibility
    bits inside an otherwise-gameplay byte); ``post`` is an optional adapter callback mutating the working
    buffer in place for rules a flat list can't express (e.g. "zero the stale projected X/Y of an *empty*
    object slot"). Out-of-range offsets are ignored so one mask can serve differently-sized captures."""
    buf = bytearray(region)
    n = len(buf)
    for o in zero:
        if 0 <= o < n:
            buf[o] = 0
    for o, m in mask:
        if 0 <= o < n:
            buf[o] &= m
    if post is not None:
        post(buf)
    return hashlib.sha1(bytes(buf)).hexdigest()


@dataclass
class TickDemo:
    """A recording keyed to game ticks.

    ``seed`` is the full VM memory image at the first tick boundary (the native bootstrap state); per tick:
    the consumed input bytes (``keys``, fixed record length), the post-tick gameplay digest (``digests``),
    and any named u16 ``sidebands`` (record-and-inject channels for state the native core cannot reproduce
    — see the module docstring)."""

    seed: bytes
    keys: list[bytes] = field(default_factory=list)          # per tick: the consumed input record
    digests: list[str] = field(default_factory=list)         # per tick: masked_digest AFTER the tick (hex)
    sidebands: dict[str, list[int]] = field(default_factory=dict)   # name -> per-tick u16 values

    _MAGIC = b"DRETICKD"     # v1: seed + keys + sha1 digests + named u16 sideband channels

    @property
    def n_ticks(self) -> int:
        return len(self.keys)

    def sideband_at(self, i: int) -> dict[str, int]:
        """The sideband values for tick ``i`` as ``{name: value}`` (missing/short channels are omitted)."""
        return {name: ch[i] for name, ch in self.sidebands.items() if i < len(ch)}

    def suffix(self, start: int, seed: bytes) -> "TickDemo":
        """A sub-demo beginning at tick ``start``, with ``seed`` as its new bootstrap state — the tick-demo
        analogue of ``InputDemoPlayback.write_suffix``. The divergence-repro workflow: when ``verify_ticks``
        reports tick ``i``, reposition a fresh state with :func:`replay_to` (fast — no digest checks),
        capture its bytes as the new seed, and save ``demo.suffix(i, seed)``: subsequent runs then reproduce
        the divergence at tick 0 instead of replaying the whole recording."""
        if not 0 <= start <= self.n_ticks:
            raise ValueError(f"suffix start {start} out of range (0..{self.n_ticks})")
        return TickDemo(seed=bytes(seed), keys=list(self.keys[start:]), digests=list(self.digests[start:]),
                        sidebands={name: list(ch[start:]) for name, ch in self.sidebands.items()})

    # --- on-disk format (a single compact, stdlib-only file) -----------------------------------------
    def save(self, path) -> None:
        """Serialize: magic, u32 zlib(seed) length + payload, u32 n_ticks, u8 key-record length, the raw
        key records, the raw 20-byte SHA1 digests, u8 sideband count, then per sideband: u8 name length,
        the UTF-8 name, and n_ticks little-endian u16 values."""
        if self.keys and any(len(k) != len(self.keys[0]) for k in self.keys):
            raise ValueError("tick demo: all key records must share one length")
        for name, ch in self.sidebands.items():
            if len(ch) != self.n_ticks:
                raise ValueError(f"tick demo: sideband {name!r} has {len(ch)} values for {self.n_ticks} ticks")
        zseed = zlib.compress(bytes(self.seed), 6)
        klen = len(self.keys[0]) if self.keys else 0
        blob = bytearray()
        blob += self._MAGIC
        blob += struct.pack("<I", len(zseed)) + zseed
        blob += struct.pack("<IB", self.n_ticks, klen)
        for k in self.keys:
            blob += k
        for d in self.digests:
            blob += bytes.fromhex(d)
        blob += struct.pack("<B", len(self.sidebands))
        for name, ch in self.sidebands.items():
            nb = name.encode("utf-8")
            blob += struct.pack("<B", len(nb)) + nb
            for v in ch:
                blob += struct.pack("<H", v & 0xFFFF)
        with open(path, "wb") as f:
            f.write(blob)

    @classmethod
    def load(cls, path) -> "TickDemo":
        raw = open(path, "rb").read()
        if raw[:8] != cls._MAGIC:
            raise ValueError(f"{path}: not a dos_re tick demo (bad magic {raw[:8]!r})")
        off = 8
        (zlen,) = struct.unpack_from("<I", raw, off); off += 4
        seed = zlib.decompress(raw[off:off + zlen]); off += zlen
        n, klen = struct.unpack_from("<IB", raw, off); off += 5
        keys = [bytes(raw[off + i * klen:off + (i + 1) * klen]) for i in range(n)]
        off += n * klen
        digests = [raw[off + i * 20:off + (i + 1) * 20].hex() for i in range(n)]
        off += n * 20
        (n_sb,) = struct.unpack_from("<B", raw, off); off += 1
        sidebands: dict[str, list[int]] = {}
        for _ in range(n_sb):
            (nlen,) = struct.unpack_from("<B", raw, off); off += 1
            name = raw[off:off + nlen].decode("utf-8"); off += nlen
            sidebands[name] = [struct.unpack_from("<H", raw, off + i * 2)[0] for i in range(n)]
            off += n * 2
        return cls(seed=seed, keys=keys, digests=digests, sidebands=sidebands)


def record_ticks(rt, *, cs: int, seed_ip: int, commit_ip: int,
                 observe: Mapping[int, Callable[[MutableMapping[str, Any], Any], None]],
                 commit: Callable[[MutableMapping[str, Any], Any], tuple[bytes, Mapping[str, int]] | None],
                 digest: Callable[[Any], str],
                 advance_one_frame: Callable[[], bool],
                 ds: int | None = None,
                 max_ticks: int = 100_000) -> TickDemo:
    """Drive an already-loaded VM ``rt`` and capture its game-tick timeline (the VM is the oracle).

    The framework owns the plumbing: it wraps ``cpu.step``, watches the adapter's seam addresses in
    segment ``cs`` (optionally gated on ``DS == ds``, which cheaply rejects same-IP hits in other
    overlays), and assembles the :class:`TickDemo`. The adapter owns the game knowledge:

    * ``seed_ip`` — the main-loop top; the FIRST time it executes, the full memory image is captured as
      the demo's seed (the native bootstrap state).
    * ``observe`` — ``{ip: callback(pending, rt)}`` capture sites. Callbacks fill/overwrite the shared
      per-tick ``pending`` dict; place them at the CONSUMPTION points (module docstring), using the
      refine pattern freely — a later site overwriting an earlier one is the intended idiom.
    * ``commit_ip`` — the end-of-tick site. ``commit(pending, rt)`` returns ``(keys, sidebands)`` to
      record the tick (the digest is taken here via ``digest(rt)``), or ``None`` to skip (e.g. the seed
      hasn't been captured yet, or the tick was a non-gameplay iteration). ``pending`` is cleared after
      every commit call.
    * ``advance_one_frame()`` — advances the VM one present-frame and returns False when the drive is
      exhausted; the caller owns pacing/input (an input-demo replay, a fast-forward driver, a live view).

    Returns the demo (possibly with 0 ticks if the seams never fired — callers should treat that as a
    seam-address bug, not an empty game)."""
    cpu = rt.cpu
    mem = cpu.mem
    out = TickDemo(seed=b"")
    pending: dict[str, Any] = {}
    seeded = [False]
    orig = cpu.step

    def sstep():
        s = cpu.s
        if (s.cs & 0xFFFF) == cs and (ds is None or (s.ds & 0xFFFF) == ds):
            ip = s.ip & 0xFFFF
            if ip == seed_ip and not seeded[0]:
                out.seed = bytes(mem.data)
                seeded[0] = True
            elif ip == commit_ip and seeded[0]:
                res = commit(pending, rt)
                if res is not None:
                    keys, sb = res
                    out.keys.append(bytes(keys))
                    for name, v in sb.items():
                        out.sidebands.setdefault(name, []).append(int(v) & 0xFFFF)
                    out.digests.append(digest(rt))
                pending.clear()
            elif seeded[0]:
                cb = observe.get(ip)
                if cb is not None:
                    cb(pending, rt)
        orig()

    cpu.step = sstep
    try:
        while out.n_ticks < max_ticks and advance_one_frame():
            pass
    finally:
        cpu.step = orig
    return out


def replay_to(demo: TickDemo, state, tick_index: int, *,
              inject: Callable[[Any, bytes, Mapping[str, int]], None],
              tick: Callable[[Any], str | None]) -> None:
    """Advance ``state`` (freshly seeded from ``demo.seed``) through ticks ``0..tick_index-1`` with NO digest
    checks — the fast repositioner for divergence repro. After it returns, ``state`` sits exactly where the
    recording stood BEFORE tick ``tick_index``; capture its bytes and carve :meth:`TickDemo.suffix` there.
    A terminal outcome or exception before ``tick_index`` means the demo/adapter changed since the verify
    run that chose the index — that is a finding, so it raises rather than repositioning wrong."""
    for i in range(tick_index):
        inject(state, demo.keys[i], demo.sideband_at(i))
        outcome = tick(state)
        if outcome is not None:
            raise RuntimeError(f"replay_to: terminal outcome at tick {i} (before target {tick_index}): {outcome}")


def verify_ticks(demo: TickDemo, state, *,
                 inject: Callable[[Any, bytes, Mapping[str, int]], None],
                 tick: Callable[[Any], str | None],
                 digest: Callable[[Any], str]) -> tuple[int, str | None]:
    """Replay ``demo`` on a VM-less native core and check it reproduces the gameplay digest every tick.

    ``state`` is the adapter's native game state, already seeded from ``demo.seed``. Per tick the loop
    calls ``inject(state, keys, sidebands)`` (write the recorded input + sideband channels into the
    state), then ``tick(state)`` — ONE native game tick. ``tick`` returns ``None`` to continue, or a
    short TERMINAL message for a transition that legitimately ends the tick-for-tick compare (level-end,
    game-over, game-complete: sequences whose VM frames have no native gameplay counterpart); an
    unrecovered path should raise (a ``HybridGap``), which is reported as the divergence.

    Returns ``(ticks_matched, divergence)`` — ``divergence`` is ``None`` when every recorded tick
    matched: the native core provably reproduced the oracle byte-for-byte (under the digest's ownership
    mask) over the whole recording."""
    for i in range(demo.n_ticks):
        inject(state, demo.keys[i], demo.sideband_at(i))
        try:
            outcome = tick(state)
        except Exception as e:                                     # noqa: BLE001 — a gap/crash IS the finding
            return i, f"tick {i}: native raised {type(e).__name__}: {str(e)[:90]}"
        if outcome is not None:
            return i, outcome
        got = digest(state)
        if got != demo.digests[i]:
            return i, f"tick {i}: gameplay digest mismatch (native {got[:12]} != recorded {demo.digests[i][:12]})"
    return demo.n_ticks, None
