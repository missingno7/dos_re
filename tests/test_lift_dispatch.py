"""dos_re.lift.dispatch: resolve near indirect-transfer targets from live CPU state.

The load-bearing tests CROSS-CHECK the pure resolver against the interpreter: set
up a near indirect jmp/call, predict its target with ``resolve_near_indirect_target``,
then actually ``step()`` the CPU and require the predicted ``CS:IP`` to equal where
the CPU landed.  This proves the resolver's addressing mirrors ``decode_ea`` for
every ModRM form -- including register-indirect (``call ax``) and BP-based EAs
whose segment defaults to SS.  All code is synthetic (no game bytes).
"""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.decode import decode_one
from dos_re.lift.dispatch import resolve_near_indirect_target
from dos_re.memory import Memory

CS = 0x1010
ENTRY = 0x0100


def _predict_then_step(code_hex: str, regs: dict, *, seed=()):
    """Load `code_hex` at CS:ENTRY, seed memory words, predict the indirect
    target from the decoded instruction + register file, then step and return
    (predicted, actual) as 'CS:IP' strings."""
    mem = Memory()
    mem.load(CS, ENTRY, bytes.fromhex(code_hex.replace(" ", "")))
    for seg, off, val in seed:
        mem.ww(seg, off, val)
    cpu = CPU8086(mem, CPUState(cs=CS, ip=ENTRY, ss=0x2000, **regs))
    inst = decode_one(lambda o: mem.rb(CS, o & 0xFFFF), ENTRY)
    predicted = resolve_near_indirect_target(cpu.s, mem, inst)
    cpu.step()
    actual = f"{cpu.s.cs & 0xFFFF:04X}:{cpu.s.ip & 0xFFFF:04X}"
    return predicted, actual


def test_register_indirect_jmp():
    # FF E3 = jmp bx ; the target IS the register value.
    pred, act = _predict_then_step("FFE3", {"bx": 0x1234})
    assert pred == "1010:1234" == act


def test_register_indirect_call():
    # FF D0 = call ax ; a computed function pointer.
    pred, act = _predict_then_step("FFD0", {"ax": 0x4E26})
    assert pred == "1010:4E26" == act


def test_memory_indirect_direct_disp16():
    # FF 26 00 02 = jmp [0x0200] ; target is the word at ds:0200.
    pred, act = _predict_then_step("FF260002", {"ds": 0x3000}, seed=[(0x3000, 0x0200, 0x587E)])
    assert pred == "1010:587E" == act


def test_memory_indirect_bx_table():
    # FF A7 34 58 = jmp [bx+0x5834] ; the video-mode jump-table idiom.
    pred, act = _predict_then_step("FFA73458", {"ds": 0x3000, "bx": 4},
                                   seed=[(0x3000, 0x5838, 0x5852)])
    assert pred == "1010:5852" == act


def test_bp_based_ea_defaults_to_ss():
    # FF 66 00 = jmp [bp+0] ; BP addressing defaults to the SS segment, not DS.
    pred, act = _predict_then_step("FF6600", {"bp": 0x0400},
                                   seed=[(0x2000, 0x0400, 0x1AEB)])  # word lives in SS=0x2000
    assert pred == "1010:1AEB" == act


def test_seg_override_honoured():
    # 2E FF 26 00 02 = jmp cs:[0x0200] ; the CS override wins over the DS default.
    pred, act = _predict_then_step("2EFF260002", {"ds": 0x3000},
                                   seed=[(CS, 0x0200, 0x0D42)])
    assert pred == "1010:0D42" == act


def test_no_modrm_returns_none():
    # C3 = ret ; nothing to resolve.
    mem = Memory()
    mem.load(CS, ENTRY, bytes.fromhex("C3"))
    inst = decode_one(lambda o: mem.rb(CS, o & 0xFFFF), ENTRY)
    cpu = CPU8086(mem, CPUState(cs=CS, ip=ENTRY))
    assert resolve_near_indirect_target(cpu.s, mem, inst) is None
