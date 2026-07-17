"""A lifted body must be re-enterable AT an INT, because a blocking host
service puts IP back there and expects the retry to re-execute it.

This is dos_re's own convention, stated in dos.py at both console-input sites:

    cpu.s.ip = (cpu.s.ip - 2) & 0xFFFF     # back ONTO the int
    raise ConsoleInputWouldBlock()

The interpreter honours it for free -- it just executes whatever is at CS:IP
again. A lifted function does not: the raise unwinds the entire Python call
chain, and the host's resume lands on an address in the middle of a lifted
block. Before the rule these tests pin, that address had no hook, so the retry
fell through to the INTERPRETER -- a VMless wall violation, and, wherever the
wall is not armed, a silent divergence with no symptom at all.

Found by skyroads' menu loop (1010:5FEB: `mov ah,07 / int 21h`), which blocks
on every single keypress wait. The emitted RESUME_ENTRIES there listed 5FEF --
the instruction AFTER the int -- which is precisely the one address that
cannot serve a retry.
"""
from __future__ import annotations

import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.dos import ConsoleInputWouldBlock
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.memory import Memory


def _build(code: bytes, name: str, **kw):
    scan = scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)
    src = emit_function(scan, 0x1010, name, signature=code[:4], **kw)
    mod = types.ModuleType(name)
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)
    return mod, getattr(mod, name), src


#: mov ah,07 ; int 21h ; and ax,00FF ; ret -- skyroads 1010:5FEB, the shape
#: that found this: a blocking DOS getch fused into its setup instruction.
GETCH = bytes.fromhex("b407" "cd21" "25ff00" "c3")


def _cpu() -> CPU8086:
    cpu = CPU8086(Memory(), CPUState(cs=0x1010, ds=0x1686, ss=0x4000,
                                     sp=0x100, flags=0x0202))
    cpu.mem.ww(0x4000, 0x0FE, 0xBEEF)      # a return address to pop
    cpu.s.sp = 0xFE
    return cpu


def test_the_int_itself_is_a_resume_entry() -> None:
    mod, _fn, _src = _build(GETCH, "getch_fn")
    assert "1010:0002" in mod.RESUME_ENTRIES, (
        "the int's OWN address must be a resume entry -- it is where a "
        f"blocking service rewinds to. got {mod.RESUME_ENTRIES}")


def test_the_int_starts_its_own_block() -> None:
    """Re-entry is only FAITHFUL if the int is a leader: resuming must
    re-execute the int alone, not the `mov ah,07` that set up the call."""
    mod, _fn, _src = _build(GETCH, "getch_leader")
    bb = mod.RESUME_ENTRIES["1010:0002"]
    assert mod.BLOCK_ADDRS[bb] == 0x0002


def test_a_blocked_int_unwinds_with_ip_on_the_int() -> None:
    """The precondition for all of this: the raise really does leave CS:IP on
    the int, so the resume address is the one RESUME_ENTRIES now exports."""
    mod, fn, _src = _build(GETCH, "getch_block")
    cpu = _cpu()

    def blocking(c, num):
        c.s.ip = (c.s.ip - 2) & 0xFFFF
        raise ConsoleInputWouldBlock()
    cpu.interrupt_handler = blocking

    try:
        fn(cpu)
    except ConsoleInputWouldBlock:
        pass
    else:
        raise AssertionError("the block did not propagate to the host")
    assert (cpu.s.cs, cpu.s.ip) == (0x1010, 0x0002)
    assert f"{cpu.s.cs:04X}:{cpu.s.ip:04X}" in mod.RESUME_ENTRIES


def test_resuming_at_the_int_retries_it_and_finishes() -> None:
    """End to end, the way the host drives it: block once, then resume at the
    exported block and let the int succeed. AH must survive the unwind (it
    lives in cpu.s), and the function must run to its ret."""
    mod, fn, _src = _build(GETCH, "getch_retry")
    cpu = _cpu()
    calls = []

    def blocking(c, num):
        calls.append((num, (c.s.ax >> 8) & 0xFF))
        if len(calls) == 1:
            c.s.ip = (c.s.ip - 2) & 0xFFFF
            raise ConsoleInputWouldBlock()
        c.s.ax = (c.s.ax & 0xFF00) | 0x41        # 'A' arrives on the retry
    cpu.interrupt_handler = blocking

    try:
        fn(cpu)
    except ConsoleInputWouldBlock:
        pass

    # the host resumes the SAME body at the block the int lives in
    fn(cpu, mod.RESUME_ENTRIES[f"{cpu.s.cs:04X}:{cpu.s.ip:04X}"])

    assert calls == [(0x21, 0x07), (0x21, 0x07)], (
        f"the retry must re-issue the same DOS call with AH intact: {calls}")
    assert cpu.s.ax == 0x0041                    # and ax,00FF ran after it
    assert cpu.s.ip == 0xBEEF                    # reached the ret


def test_the_rule_does_not_need_boundary_observation() -> None:
    """A body with no boundary heads, no dispatch entries and resume_calls off
    still gets its int re-entry: blocking is independent of frame pacing, and
    a port that has not adopted boundary observation blocks just the same."""
    mod, _fn, _src = _build(GETCH, "getch_plain", boundary_heads=frozenset(),
                            dispatch_entries=frozenset(), resume_calls=False)
    assert mod.RESUME_ENTRIES == {"1010:0002": 1}


def test_an_int_free_body_gains_no_resume_entries() -> None:
    """The other direction: the rule costs nothing where it does not apply."""
    mod, _fn, src = _build(bytes.fromhex("31c0" "c3"), "no_int_fn")
    assert not hasattr(mod, "RESUME_ENTRIES")
    assert "RESUME_ENTRIES" not in src
