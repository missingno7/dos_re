"""De-SMC'd far-control-flow ISR chain through the timer-handler tail.

A recovered interrupt handler ends by chaining (``jmp far``) to the PREVIOUS
owner of its vector.  The ISR installer patched that far ptr16:16 into the
instruction at hook time (de-SMC ``far-target`` slot), and the chained handler
is OUTSIDE the recovered corpus (the prior INT owner -- the BIOS IRET stub, or
a TSR).  So the chain is emitted as an EXPLICIT platform effect
(``plat.chain_interrupt``) that reads the runtime target from live CODE memory,
NEVER as a recovered-code (HANDLERS/_ivec) dispatch.  The standalone runtime
models the BIOS IRET stub as a verified no-op; any other target fails loud.
"""
from __future__ import annotations

from dataclasses import replace as _dc_replace

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.cpuless import register_effects
from dos_re.lift.emit_cpuless import (_is_desmc_far_chain, _is_isr_chain,
                                      check_promotable, emit_recovered)
from dos_re.lift.platform import (CPUlessPlatformRuntime, UnsupportedPlatformEffect,
                                  _chain_iret_stub)
from dos_re.memory import Memory


# `jmp far F000:FF53` (EA 53 FF 00 F0), with the ptr16:16 operand at ip+1 marked
# a runtime-patched de-SMC far-target slot -- the ISR chain tail.
def _chain_inst(ip=0):
    i = decode_one(lambda o: bytes.fromhex("ea53ff00f0")[o], ip)
    return _dc_replace(i, patched_slot=("far-target", ip + 1, 4))


def test_predicates_recognise_the_desmc_far_chain() -> None:
    i = _chain_inst()
    assert _is_desmc_far_chain(i)
    assert _is_isr_chain(i)            # it IS an ISR chain tail (iret exit kind)
    # a NON-patched direct far jmp is a plain jmp, not an ISR chain.
    plain = decode_one(lambda o: bytes.fromhex("ea53ff00f0")[o], 0)
    assert not _is_desmc_far_chain(plain)
    assert not _is_isr_chain(plain)


def test_abi_gives_the_chain_full_bundle_isr_dataflow() -> None:
    # the patched far chain reads/writes the full register bundle (the chained
    # handler runs on our frame) -- like the FF /5 form, unlike a plain jmp.
    e = register_effects(_chain_inst())
    assert e.refusal is None
    assert {"ax", "bx", "cx", "dx"} <= e.reads
    assert "ss" not in e.writes


def test_emitter_routes_the_chain_through_plat_not_ivec() -> None:
    scan = FunctionScan(entry=0)
    scan.insts[0] = _chain_inst(0)
    spec = check_promotable(scan)
    assert spec.ret_kind == "iret"        # an ISR handler
    src = emit_recovered(scan, spec.abi, "1010:3B17", needs_plat=spec.needs_plat,
                         flags_livein=spec.flags_livein)
    # target read from CODE memory (cs:[operand]), dispatched via the platform.
    assert "mem.rw(cs, 0x0001)" in src        # ptr16:16 offset word
    assert "mem.rw(cs, 0x0003)" in src        # ptr16:16 segment word
    assert "plat.chain_interrupt(" in src
    assert "_ivec(" not in src                # NOT recovered-code dispatch
    assert "from" not in src or "_dyncall" not in src   # no _ivec import
    compile(src, "<chain>", "exec")


def test_chain_iret_stub_no_ops_on_an_iret_target() -> None:
    mem = Memory()
    # F000:FF53 is BIOS ROM (wb is a no-op there, as on real hardware); seed the
    # iret stub physically, exactly as the boot image does at build time.
    mem.wb_phys(0xF000 * 16 + 0xFF53, 0xCF)
    regs = {"ax": 0x1234, "_flags_in": 0x2}
    out, compat = _chain_iret_stub(mem, 0xF000, 0xFF53, regs)
    assert out["ax"] == 0x1234            # bundle passes through unchanged
    assert compat["fmask"] == 0 and compat["cost"] == 0


def test_chain_to_a_non_iret_target_fails_loud() -> None:
    mem = Memory()
    mem.wb(0x1234, 0x0010, 0x55)          # NOT an iret -> unmodelled external
    with pytest.raises(UnsupportedPlatformEffect, match="not the BIOS IRET stub"):
        _chain_iret_stub(mem, 0x1234, 0x0010, {"_flags_in": 2})


def test_standalone_runtime_exposes_chain_interrupt() -> None:
    mem = Memory()
    mem.wb_phys(0xF000 * 16 + 0xFF53, 0xCF)
    rt = CPUlessPlatformRuntime(mem, game_root=None, dos=object())
    out, compat = rt.chain_interrupt(0xF000, 0xFF53, {"bx": 7, "_flags_in": 2}, 3)
    assert out["bx"] == 7
