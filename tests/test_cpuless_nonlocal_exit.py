"""setjmp/longjmp: the stack-pointer SNAPSHOT and the NON-LOCAL EXIT.

A DOS C runtime that offers setjmp/longjmp saves the stack pointer into a
memory slot (``mov m16, sp``) and, on a fatal path, restores it and returns
(``mov sp, m16 ; ... ; ret``) -- returning on a frame the returning function
never established.  Both trip the general ``sp-as-data`` refusal, and the
second one dos_re genuinely could not REPRESENT: the CPUless model holds
frames on the host's Python stack.

The split adopted here, and what each half rests on:

* the SNAPSHOT is permitted -- the ABI keeps ``sp`` exact, so storing it is an
  ordinary exact store and the function's own ``sp`` is unchanged.  Nothing is
  assumed about who reads the slot back;
* the RESTORE is TERMINAL -- it emits a fail-loud raise and records no exit
  ABI, exactly as a runtime-dead exit does.  The UNWIND IS NOT MODELLED:
  faking a plain return there would hand the immediate Python caller a bogus
  depth and corrupt every caller's accounting *silently*, which is strictly
  worse than refusing.

Both recognisers are TIGHT, in the manner of the bootstrap ss-switch: anything
looser is ordinary sp-as-data and still refuses.  Byte sequences below are the
real ones from OVERKILL (``1010:02BC``, ``1010:0011``); no other lifted corpus
in the ecosystem contains either shape, so this is a dos_re capability gap the
one game happens to expose -- not a per-game quirk encoded into the lifter.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import (Refusal, _is_nonlocal_exit,
                                      _is_sp_snapshot_store, check_promotable,
                                      emit_recovered)


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


# OVERKILL 1010:065C's error tail, minimised:
#   jnb +5 ; mov sp, cs:[0242] ; ret
# the taken branch is the ordinary return; the fallthrough is the longjmp.
_LONGJMP = bytes.fromhex("7305" "2e8b264202" "c3")
_RESTORE_AT = 0x02

# OVERKILL 1010:0011's setjmp save: `mov ds:[000E], sp ; ret`
_SETJMP = bytes.fromhex("89260e00" "c3")


def test_recognises_the_restore_into_a_return_only() -> None:
    lj = _scan(_LONGJMP)
    assert _is_nonlocal_exit(lj, lj.insts[_RESTORE_AT])
    # `mov sp, ax` -- a REGISTER source is ordinary sp-as-data, not a longjmp.
    reg = _scan(bytes.fromhex("8be0" "c3"))
    assert not _is_nonlocal_exit(reg, reg.insts[0])
    # a restore with a PUSH before the return is not a bare abort tail: real
    # stack traffic on the restored frame is exactly what we cannot model.
    pushy = _scan(bytes.fromhex("2e8b264202" "50" "c3"))
    assert not _is_nonlocal_exit(pushy, pushy.insts[0])
    # a restore falling into `ret 4` pops a frame we do not model either.
    retn = _scan(bytes.fromhex("2e8b264202" "c20400"))
    assert not _is_nonlocal_exit(retn, retn.insts[0])


def test_recognises_the_snapshot_store_only() -> None:
    sj = _scan(_SETJMP)
    assert _is_sp_snapshot_store(sj.insts[0])
    # `mov di, sp` parks sp in a general register -- arbitrary arithmetic may
    # follow, which is the sp-as-data the refusal exists to catch.
    reg = _scan(bytes.fromhex("89e7" "c3"))
    assert not _is_sp_snapshot_store(reg.insts[0])


def test_setjmp_snapshot_promotes_and_stores_the_exact_sp() -> None:
    spec = check_promotable(_scan(_SETJMP, exits=(4,)))   # must NOT raise
    src = emit_recovered(_scan(_SETJMP, exits=(4,)), spec.abi, "1010:0011")
    assert "sp" in src                      # the tracked sp is what is stored
    compile(src, "<setjmp>", "exec")


def test_longjmp_promotes_and_emits_a_fail_loud_raise() -> None:
    lj = _scan(_LONGJMP, exits=(0x07,))
    spec = check_promotable(lj)             # must NOT raise
    src = emit_recovered(lj, spec.abi, "1010:065C")
    assert "non-local exit (longjmp) at 1010:0002 taken" in src
    # it must RAISE, never fall through to a fabricated return on the
    # restored frame -- and the restored sp must never reach the local.
    assert "raise RuntimeError" in src
    compile(src, "<longjmp>", "exec")


def test_the_longjmp_tail_contributes_no_exit_abi() -> None:
    """The terminal restore must not record a fictional exit contract.

    The ordinary `ret` at 0x07 is reached BOTH by the taken branch (a real,
    balanced exit) and, statically, by falling through the restore.  If the
    depth walk propagated through the restore, that second arrival would
    record an exit depth derived from a frame this function never built.
    """
    lj = _scan(_LONGJMP, exits=(0x07,))
    spec = check_promotable(lj)
    # exactly one real exit, and it is balanced -- the longjmp path added none.
    # If the walk had propagated through the restore, the ret would have been
    # reached at a second, foreign depth and sp_deltas would carry it.
    assert spec.sp_deltas == (0,)
    assert spec.sp_delta == 0
    assert not spec.sp_output


def test_a_bare_sp_load_from_memory_still_refuses() -> None:
    # `mov sp, cs:[0242] ; push ax ; ret` -- reads sp back into circulation
    # rather than aborting, so it stays sp-as-data.
    with pytest.raises(Refusal, match="sp-as-data"):
        check_promotable(_scan(bytes.fromhex("2e8b264202" "50" "c3"),
                               exits=(6,)))
