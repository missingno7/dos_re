"""The C-startup bootstrap: the one sanctioned SS mutation + sp-as-data.

A Borland/Turbo C program's entry point (skyroads 1010:61F3) relocates the
program stack with an atomic ``cli ; mov ss, reg ; <sp write> ; sti`` switch and
then computes a fresh sp as it sets the stack/heap up.  The general CPUless ABI
refuses both (``cs-or-ss-mutation``, ``sp-as-data``) because a changing (ss:sp)
breaks composition -- but a BOOTSTRAP owns the (ss:sp) pair, never returns
through a frame (it transfers to the game and terminates), so those are
runtime-owned locals there.  The relaxation is confined to the bootstrap (the
ss-switch marker); a stray ``mov ss`` or sp-as-data ANYWHERE else still refuses.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import (Refusal, _is_bootstrap_ss_switch,
                                      check_promotable, emit_recovered)


def _scan(code: bytes, exits=()) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90  # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        if ip in exits:
            s.exits.append(i)
        ip = i.next_ip
    return s


# cli ; mov ss,di ; add sp,0x100 ; sti ; sub sp,cx ; ret
_BOOT = bytes.fromhex("fa" "8ed7" "81c40001" "fb" "2be1" "c3")


def test_recognises_the_atomic_stack_switch_only() -> None:
    boot = _scan(_BOOT)
    ss_at = 0x0001                          # `mov ss, di` (after the cli)
    assert _is_bootstrap_ss_switch(boot, boot.insts[ss_at])
    # a bare `mov ss, di ; ret` (no cli bracket, no paired sp write) is NOT it.
    bare = _scan(bytes.fromhex("8ed7" "c3"))
    assert not _is_bootstrap_ss_switch(bare, bare.insts[0])


def test_bootstrap_promotes_with_ss_switch_and_sp_as_data() -> None:
    boot = _scan(_BOOT, exits=(len(_BOOT) - 1,))
    spec = check_promotable(boot)           # must NOT raise
    src = emit_recovered(boot, spec.abi, "1010:61F3")
    assert "ss = di" in src                 # the relocation reassigns the local
    assert "_a = sp" in src and "sp = _t & 0xFFFF" in src  # sp computed as data
    compile(src, "<boot>", "exec")


def test_non_bootstrap_ss_write_still_refuses() -> None:
    # `mov ss, di` with no cli/sp-write bracket -> a general segment mutation.
    with pytest.raises(Refusal, match="cs-or-ss-mutation"):
        check_promotable(_scan(bytes.fromhex("8ed7" "c3"), exits=(2,)))


def test_non_bootstrap_sp_as_data_still_refuses() -> None:
    # `sub sp, cx ; ret` with no ss-switch -> sp used as general data, refused.
    with pytest.raises(Refusal, match="sp-as-data"):
        check_promotable(_scan(bytes.fromhex("2be1" "c3"), exits=(2,)))
