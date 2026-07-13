"""Unit tests for the per-instance step observer (dos_re.step_probe).

A fake CPU with the same surface the module touches (``.s.cs/.s.ip``, a class-level ``step``)
proves the trap filtering, the entry (before-execute) semantics, instance isolation, and the
LIFO nesting/restore contract — no real interpreter needed for what is being proven here."""
from __future__ import annotations

import pytest

from dos_re.step_probe import install_step_observer, step_observer


class _State:
    def __init__(self):
        self.cs = 0x1010
        self.ip = 0x0000


class _FakeCPU:
    def __init__(self):
        self.s = _State()
        self.executed = 0

    def step(self):
        self.executed += 1
        self.s.ip = (self.s.ip + 1) & 0xFFFF


def test_untrapped_observer_sees_every_step():
    cpu = _FakeCPU()
    seen = []
    with step_observer(cpu, lambda c: seen.append(c.s.ip)):
        for _ in range(5):
            cpu.step()
    assert seen == [0, 1, 2, 3, 4]
    assert cpu.executed == 5


def test_trap_fires_only_at_trapped_addresses_before_execution():
    cpu = _FakeCPU()
    seen = []
    with step_observer(cpu, lambda c: seen.append((c.s.cs, c.s.ip)), trap={(0x1010, 3)}):
        for _ in range(6):
            cpu.step()
    # fired exactly once, at entry (ip still 3 — the instruction had not executed yet)
    assert seen == [(0x1010, 3)]
    assert cpu.executed == 6


def test_trap_masks_addresses_to_16_bits():
    cpu = _FakeCPU()
    seen = []
    with step_observer(cpu, lambda c: seen.append(c.s.ip), trap={(0x11010, 0x10002)}):
        for _ in range(4):
            cpu.step()
    assert seen == [2]


def test_uninstall_restores_the_plain_class_step():
    cpu = _FakeCPU()
    uninstall = install_step_observer(cpu, lambda c: None)
    assert "step" in cpu.__dict__
    uninstall()
    assert "step" not in cpu.__dict__
    cpu.step()
    assert cpu.executed == 1


def test_observers_nest_and_unwind_lifo():
    cpu = _FakeCPU()
    order = []
    un_outer = install_step_observer(cpu, lambda c: order.append("outer"))
    un_inner = install_step_observer(cpu, lambda c: order.append("inner"))
    cpu.step()
    assert order == ["inner", "outer"]      # inner wraps outer wraps the class step
    un_inner()
    cpu.step()
    assert order == ["inner", "outer", "outer"]
    un_outer()
    assert "step" not in cpu.__dict__


def test_out_of_order_uninstall_fails_loud():
    cpu = _FakeCPU()
    un_outer = install_step_observer(cpu, lambda c: None)
    install_step_observer(cpu, lambda c: None)
    with pytest.raises(RuntimeError):
        un_outer()


def test_instances_are_isolated():
    a, b = _FakeCPU(), _FakeCPU()
    seen = []
    with step_observer(a, lambda c: seen.append("a")):
        a.step()
        b.step()
    assert seen == ["a"]
    assert (a.executed, b.executed) == (1, 1)
