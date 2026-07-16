"""Flag-liveness analysis + dead-flag elision — de-carrier pass 1.

House style: the analysis is validated structurally (which sites it marks
dead) AND differentially (the elided emission must remain byte-exact against
the interpreted original, final flags included — guaranteed by the
exit-live/seam-live convention).  Synthetic code only.
"""
from __future__ import annotations

import random

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.analyze import dead_flag_sites
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.memory import Memory

CS = 0x1000
ENTRY = 0x0100
RET_IP = 0xBEEF


def _scan(code: bytes):
    fetch = lambda off: code[(off - ENTRY) & 0xFFFF] if 0 <= (off - ENTRY) < len(code) else 0x90
    return scan_function(fetch, ENTRY)


def test_interior_flags_dead_exit_flags_live():
    # add ; add ; ret — the first add's flags are guaranteed overwritten by
    # the second before the exit; the second's flags escape at ret.
    code = bytes.fromhex("01D8" "01C8" "C3")
    dead = dead_flag_sites(_scan(code))
    assert 0x0100 in dead                     # interior add: dead
    assert 0x0102 not in dead                 # final add: live at exit


def test_seams_keep_flags_live():
    # add ; call ; add ; ret — the first add's flags are observable at the
    # call seam (a boundary park can expose full state there).
    code = bytes.fromhex("01D8" "E80300" "01C8" "C3"
                         "90"                     # pad
                         "C3")                    # callee: ret
    dead = dead_flag_sites(_scan(code))
    assert 0x0100 not in dead


def test_adc_keeps_producer_alive():
    # sub ; adc ; add ; ret — sub's CF is read by adc, so sub is live; adc's
    # flags are overwritten by add before the exit, so adc is dead.
    code = bytes.fromhex("29D8" "11C8" "01D8" "C3")
    dead = dead_flag_sites(_scan(code))
    assert 0x0100 not in dead                 # sub: CF consumed by adc
    assert 0x0102 in dead                     # adc: overwritten by add
    assert 0x0104 not in dead                 # add: exit-live


def test_jcc_consumes_condition_flags():
    # cmp ; jz +1 ; inc ax ; add ; ret — cmp feeds the jz (live); the inc's
    # five flags are overwritten by add (dead; and CF is untouched by inc,
    # but the following ADD kills CF anyway before the exit).
    code = bytes.fromhex("39D8" "7401" "40" "01C8" "C3")
    dead = dead_flag_sites(_scan(code))
    assert 0x0100 not in dead                 # cmp: jz reads ZF
    assert 0x0104 in dead                     # inc: overwritten by add
    assert 0x0105 not in dead                 # add: exit-live


def test_elided_emission_stays_byte_exact():
    # Differential: emit WITH elision, run against the interpreted original
    # from randomized states — registers, memory AND final flags must match
    # (exit-liveness keeps the escaping flags), and virtual time is equal.
    code = bytes.fromhex(
        "01D8"        # 0100 add ax,bx      (dead: overwritten below)
        "29D8"        # 0102 sub ax,bx      (dead)
        "85DB"        # 0104 test bx,bx     (dead)
        "40"          # 0106 inc ax         (dead five; CF killed by 0107)
        "01C8"        # 0107 add ax,cx      (live: jz reads ZF)
        "7401"        # 0109 jz +1
        "43"          # 010B inc bx         (LIVE: xor keeps AF, so the inc's
                      #                      AF write survives to the exit)
        "31D2"        # 010C xor dx,dx      (live at exit)
        "C3")         # 010E ret
    scan = _scan(code)
    dead = frozenset(dead_flag_sites(scan))
    assert {0x0100, 0x0102, 0x0104, 0x0106} <= dead
    assert 0x010B not in dead                 # AF escapes through the xor
    assert 0x0107 not in dead and 0x010C not in dead

    src = emit_function(scan, CS, "lifted", signature=code[:8],
                        count_instructions=True, dead_flag_ips=dead)
    assert src.count("set_") < emit_function(
        scan, CS, "lifted", signature=code[:8],
        count_instructions=True).count("set_")
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102
    lifted = ns["lifted"]

    rng = random.Random(0xF1A6)
    for case in range(80):
        st = dict(ax=rng.randrange(0x10000), bx=rng.randrange(0x10000),
                  cx=rng.randrange(0x10000), dx=rng.randrange(0x10000),
                  si=0, di=0, bp=0, sp=0x2000, cs=CS, ip=ENTRY,
                  ds=0x4000, es=0x4000, ss=0x3000,
                  flags=(rng.getrandbits(16) & 0x0CD5) | 0x0202)
        cpus = []
        for _ in range(2):
            mem = Memory()
            mem.load(CS, ENTRY, code)
            cpu = CPU8086(mem, CPUState(**st))
            cpu.trace_enabled = False
            cpu.push(RET_IP)
            cpus.append(cpu)
        asm, hook = cpus
        for _ in range(200):
            if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
                break
            asm.step()
        lifted(hook)
        assert (hook.s.cs, hook.s.ip) == (CS, RET_IP)
        assert asm.s.snapshot() == hook.s.snapshot(), f"case {case}\n{src}"
        assert asm.mem.data == hook.mem.data
        assert asm.instruction_count == hook.instruction_count
