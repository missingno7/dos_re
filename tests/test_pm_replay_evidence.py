"""PMFunctionObserver semantics -- the shadow-stack rules the debugger earned.

Each case is a real failure mode observed on the first PM corpus (Krypton
Egg): sibling calls from one loop site leaking frames, stack-switching ISRs
breaking a global LIFO, and jump-entries (loop heads at function entries)
counted as invocations.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.pm_replay_evidence import PMFunctionObserver  # noqa: E402
from dos_re.replay import ReplayEvidenceRecorder, ReplayPoint  # noqa: E402

TL = "test-timeline:v1"


class _Mem:
    def __init__(self):
        self.data = bytearray(0x200000)

    def r32(self, a):
        return int.from_bytes(self.data[a:a + 4], "little")

    def r8(self, a):
        return self.data[a]

    def w32(self, a, v):
        self.data[a:a + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")


class _CPU:
    def __init__(self):
        self.r = [0] * 8
        self.eip = 0
        self.mem = _Mem()
        self.entry_probes = None


def _setup(entries):
    cpu = _CPU()
    rec = ReplayEvidenceRecorder()
    frame = [0]
    obs = PMFunctionObserver(cpu, entries, rec,
                             lambda: ReplayPoint(frame[0], TL))
    return cpu, rec, obs, frame


def _call(cpu, obs, entry, esp, ret):
    """Simulate the machine state at a probed CALL entry and fire the probe."""
    # a direct near call E8 rel32 ending at `ret`, targeting `entry`
    cpu.mem.data[ret - 5] = 0xE8
    cpu.mem.w32(ret - 4, (entry - ret) & 0xFFFFFFFF)
    cpu.mem.w32(esp, ret)
    cpu.r[4] = esp
    cpu.eip = entry
    obs._fire(cpu)


def test_sibling_loop_calls_do_not_leak():
    """N calls from one site must yield N invocations and N-1 exits inline."""
    cpu, rec, obs, frame = _setup({0x1000: "fn_a"})
    for i in range(50):
        frame[0] = i
        _call(cpu, obs, 0x1000, esp=0x8FF0, ret=0x5005)
    stacks = [len(s) for s in obs._stacks.values()]
    assert sum(stacks) == 1              # only the live invocation remains
    cpu.r[4] = 0x9000                    # the final call returned to mainline
    obs.finish()
    visit = rec.visits.records()[0]
    assert visit.invocation_count == 50
    assert not visit.incomplete
    assert visit.first_entry.ordinal == 0
    assert visit.last_exit.ordinal == 49   # closed at the final boundary


def test_nested_calls_stay_live_until_unwound():
    cpu, rec, obs, frame = _setup({0x1000: "outer", 0x2000: "inner"})
    _call(cpu, obs, 0x1000, esp=0x8FF0, ret=0x5005)   # outer
    _call(cpu, obs, 0x2000, esp=0x8FE0, ret=0x6005)   # inner, deeper
    assert sum(len(s) for s in obs._stacks.values()) == 2
    frame[0] = 3
    # a new sibling of OUTER at the same depth unwinds both inner and outer
    _call(cpu, obs, 0x1000, esp=0x8FF0, ret=0x5005)
    records = {v.function_id: v for v in rec.visits.records()}
    assert records["inner"].invocation_count == 1
    assert not records["inner"].incomplete
    assert records["outer"].invocation_count == 2
    # observed transfer outer -> inner
    transfers = {(t.source_id, t.target_id) for t in rec._transfers.values()}
    assert ("outer", "inner") in transfers


def test_stack_switch_frames_close_at_finish():
    """ISR frames on another stack must not block mainline unwinding."""
    cpu, rec, obs, frame = _setup({0x1000: "main_fn", 0x3000: "isr_fn"})
    _call(cpu, obs, 0x1000, esp=0x8FF0, ret=0x5005)     # mainline stack 0x0
    _call(cpu, obs, 0x3000, esp=0x7FFF0, ret=0x7005)    # ISR stack 0x7
    assert len(obs._stacks) == 2
    frame[0] = 2
    _call(cpu, obs, 0x1000, esp=0x8FF0, ret=0x5005)     # mainline sibling
    # the ISR frame is untouched by mainline unwinding...
    isr_stack = obs._stacks[0x7FFF0 >> 16]
    assert len(isr_stack) == 1
    # ...and closes at the quiescent frame boundary
    cpu.r[4] = 0x8FF0
    obs.finish()
    records = {v.function_id: v for v in rec.visits.records()}
    assert not records["isr_fn"].incomplete


def test_jump_entries_are_not_invocations():
    """A loop jumping at a probed entry must not count or push frames."""
    cpu, rec, obs, frame = _setup({0x1000: "fn_a"})
    # stack top holds a value whose preceding bytes are NOT a call form
    cpu.mem.w32(0x8FF0, 0x5005)          # bytes at 0x5000..0x5004 are zero
    cpu.r[4] = 0x8FF0
    cpu.eip = 0x1000
    obs._fire(cpu)
    assert not rec.visits.records()
    assert sum(len(s) for s in obs._stacks.values()) == 0


def test_probes_are_read_only_after_construction():
    """The per-instruction fast path must never see the probe dict mutate."""
    cpu, rec, obs, frame = _setup({0x1000: "fn_a", 0x2000: "fn_b"})
    before = dict(cpu.entry_probes)
    for i in range(10):
        frame[0] = i
        _call(cpu, obs, 0x1000, esp=0x8FF0 - (i % 3) * 8, ret=0x5005)
        _call(cpu, obs, 0x2000, esp=0x8FC0, ret=0x6005)
    assert cpu.entry_probes == before
