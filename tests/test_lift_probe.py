"""The IP-delta probe must not corrupt the code it is measuring.

The probe executes each candidate instruction for real, at a forced IP, with whatever registers the
previous probe step left behind. Those registers are meaningless, so a store instruction writes to an
essentially arbitrary address -- and when that address lands in the CODE SEGMENT, every later probe
decodes the overwritten bytes. The mismatch it then reports is an artifact of the probe.

This is not a hypothetical hazard: it silently refused a real function (OVERKILL's `1010:0248`) with
three bogus ``decoder-mismatch`` refusals, all reporting ``interpreter-delta=3`` because the clobbered
region read `3D FF 3D FF ...` and `3D` is a 3-byte `cmp ax,imm16`. That refusal cascaded far enough to
block the game's entire top-level loop from promotion.
"""
from __future__ import annotations

import types

from dos_re.lift.probe import make_ip_delta_probe

CS = 0x1010
BASE = CS << 4


class _Mem:
    def __init__(self, data):
        self.data = bytearray(data)

    def rb(self, seg, off):
        return self.data[((seg << 4) + (off & 0xFFFF)) & 0xFFFFF]

    def wb(self, seg, off, val):
        self.data[((seg << 4) + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF


class _State:
    cs = 0
    ip = 0


class _CPU:
    """A stand-in interpreter whose `step` advances IP by the byte at IP (so a test can assert an
    exact delta) and, at one chosen address, also SCRIBBLES over the code segment."""

    def __init__(self, mem, scribble_at=None):
        self.mem = mem
        self.s = _State()
        self.replacement_hooks = {}
        self.hook_names = {}
        self.hook_verifier = object()
        self.trace_enabled = True
        self.pending_irq = object()
        self._scribble_at = scribble_at

    def step(self):
        ip = self.s.ip
        length = self.mem.rb(self.s.cs, ip) or 1
        if self._scribble_at is not None and ip == self._scribble_at:
            for off in range(0x0100, 0x0140):          # a stray store into CODE
                self.mem.wb(self.s.cs, off, 0x3D)
        self.s.ip = (ip + length) & 0xFFFF


def _rt(scribble_at=None):
    data = bytearray(0x100000)
    for off in range(0x0100, 0x0140):
        data[BASE + off] = 2                            # every instruction is 2 bytes long
    data[BASE + 0x0130] = 5                             # except this one
    cpu = _CPU(_Mem(data), scribble_at=scribble_at)
    return types.SimpleNamespace(cpu=cpu)


def _clone_passthrough(monkeypatch):
    """make_ip_delta_probe clones the runtime; the fake has no cloning machinery, so pass it
    through. The behaviour under test is the memory restore, not the clone."""
    import dos_re.snapshot as ra
    monkeypatch.setattr(ra, "clone_runtime_state", lambda rt: rt)


def test_probe_reports_true_instruction_lengths(monkeypatch):
    _clone_passthrough(monkeypatch)
    probe = make_ip_delta_probe(_rt(), CS)
    assert probe(0x0110) == 2
    assert probe(0x0130) == 5


def test_probe_restores_code_the_step_scribbled_over(monkeypatch):
    """The regression: a step that writes into the code segment must not change what LATER probes
    decode. Without the restore, 0x0130 reads 0x3D and reports 3 instead of its true 5."""
    _clone_passthrough(monkeypatch)
    rt = _rt(scribble_at=0x0110)
    probe = make_ip_delta_probe(rt, CS)

    assert probe(0x0110) == 2          # this step scribbles 0x3D across 0x0100..0x013F
    assert probe(0x0130) == 5, "a later probe must still see the ORIGINAL code bytes"
    assert rt.cpu.mem.rb(CS, 0x0130) == 5, "the code segment must be pristine after probing"


def test_without_restore_the_corruption_is_observable(monkeypatch):
    """Pins that the hazard is real and that `restore` is what prevents it -- so this test fails if
    someone 'optimises' the restore away."""
    _clone_passthrough(monkeypatch)
    rt = _rt(scribble_at=0x0110)
    probe = make_ip_delta_probe(rt, CS, restore=False)

    assert probe(0x0110) == 2
    # The fake advances IP by the byte AT ip, so the scribbled 0x3D reads back as 61 rather than the
    # real decoder's 3 for `cmp ax,imm16`. The number is an artifact of the fake; what matters is
    # that the probe now measures its own scribble instead of the instruction's true length of 5.
    assert probe(0x0130) == 0x3D
    assert probe(0x0130) != 5, "unrestored, the probe reports the scribble, not the real length"


def test_probe_neutralises_hooks_tracing_and_irqs(monkeypatch):
    """A hook, a trace or a pending IRQ would each make `step` do something other than 'execute this
    one instruction', which is the only thing the delta means."""
    _clone_passthrough(monkeypatch)
    rt = _rt()
    rt.cpu.replacement_hooks["x"] = object()
    make_ip_delta_probe(rt, CS)
    assert not rt.cpu.replacement_hooks and not rt.cpu.hook_names
    assert rt.cpu.hook_verifier is None
    assert rt.cpu.trace_enabled is False
    assert rt.cpu.pending_irq is None


def test_probe_returns_none_when_the_step_faults(monkeypatch):
    _clone_passthrough(monkeypatch)
    rt = _rt()

    def boom():
        raise RuntimeError("cannot execute here")

    rt.cpu.step = boom
    assert make_ip_delta_probe(rt, CS)(0x0110) is None
