"""A lifted function that will not terminate must SAY WHY, and only when true.

The old guard did neither. It killed at a fixed iteration count -- so a long
honest loop (a decompressor, a full-screen blit) died exactly like a real
spin, and the only way past it was to keep raising a magic number until the
number stopped mattering. And when it fired, it said:

    exceeded MAX_ITERATIONS (unbounded internal loop -- likely an environment
    wait; hook it by hand)

which names no address, proves nothing, and whose advice ("hook it by hand")
is usually not the fix -- declaring a boundary head is. That guess cost this
project hours on two separate loops before anyone doubted it.

The replacement is a claim that can be checked: a spin returns to the SAME
dispatch block with IDENTICAL registers, which is provably no progress; a
loop whose registers advance is never reported. These tests pin both
directions, because a detector that only catches spins is half a detector --
the false-positive half is what makes it safe to relax the guard at all.
"""
from __future__ import annotations

import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.lift.runtime import LiftRuntimeError, LiftStuck
from dos_re.memory import Memory


def _build(code: bytes, name: str, *, iters: int = 5_000_000):
    scan = scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)
    src = emit_function(scan, 0x1010, name, signature=code[:4], min_iterations=iters)
    mod = types.ModuleType(name)
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)
    return mod, getattr(mod, name), src


#: cmp ds:[1600],ax ; jz -6 ; ret  -- the tick-wait shape (skyroads 47CD/22F8/4468)
SPIN = bytes.fromhex("39060016" "74fa" "c3")
#: lodsb ; stosb ; dec cx ; jnz -5 ; ret  -- the decompressor shape
DECODER = bytes.fromhex("ac" "aa" "49" "75fb" "c3")


def _cpu(**kw) -> CPU8086:
    st = dict(cs=0x1010, ds=0x1686, es=0x3000, ss=0x4000, sp=0x100, flags=0x0202)
    st.update(kw)
    return CPU8086(Memory(), CPUState(**st))


def test_a_spin_is_detected_and_names_where() -> None:
    mod, fn, _ = _build(SPIN, "spin_fn")
    cpu = _cpu()
    cpu.mem.ww(0x1686, 0x1600, 0)
    cpu.s.ax = 0                       # tick == ax -> the jz loops forever
    try:
        fn(cpu)
    except LiftStuck as exc:
        msg = str(exc)
    else:
        raise AssertionError("a provable spin was not detected")
    assert "STUCK at 1010:0000" in msg          # the address, not "somewhere"
    assert "no progress" in msg
    assert "boundary-heads" in msg              # the fix that is actually right
    assert "1010:0000" in msg.rsplit("declare:", 1)[-1]


def test_a_spin_is_caught_long_before_the_guard() -> None:
    """The point of detecting rather than counting: it fires in ~64K, not 5M."""
    mod, fn, _ = _build(SPIN, "spin_fast", iters=50_000_000)
    cpu = _cpu()
    cpu.mem.ww(0x1686, 0x1600, 0)
    cpu.s.ax = 0
    try:
        fn(cpu)
    except LiftStuck as exc:
        assert "65,536 iterations" in str(exc)   # not 50,000,000
    else:
        raise AssertionError("not detected")


def test_a_long_honest_loop_is_NOT_reported_stuck() -> None:
    """The half that lets the guard be relaxed: 200k iterations of real work,
    far past the sampler, must run to completion untouched."""
    mod, fn, _ = _build(DECODER, "decoder_fn")
    cpu = _cpu(ds=0x2000)
    cpu.s.cx = 200_000
    fn(cpu)                                    # must not raise
    assert cpu.s.cx == 0                       # it really did run the loop out


def test_stuck_is_a_liftruntimeerror_subclass() -> None:
    # Existing `except LiftRuntimeError` sites must keep catching it: a hook
    # that cannot finish is still a hard failure, whatever we now call it.
    assert issubclass(LiftStuck, LiftRuntimeError)


def test_disabled_interrupts_are_called_out() -> None:
    """IF=0 in a wait loop is a contradiction worth naming: no ISR can run, so
    nothing the host delivers can ever release it."""
    mod, fn, _ = _build(SPIN, "spin_cli")
    cpu = _cpu(flags=0x0002)                   # IF clear
    cpu.mem.ww(0x1686, 0x1600, 0)
    cpu.s.ax = 0
    try:
        fn(cpu)
    except LiftStuck as exc:
        assert "IF=0" in str(exc)
        assert "interrupts are DISABLED" in str(exc)
    else:
        raise AssertionError("not detected")


def test_emitted_module_carries_the_block_address_map() -> None:
    mod, _fn, src = _build(SPIN, "spin_addrs")
    assert mod.BLOCK_ADDRS[0] == 0x0000
    assert "PROGRESS_SAMPLE" in src
    # the old guess must be gone from generated code
    assert "hook it by hand" not in src
