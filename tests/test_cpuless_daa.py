"""DAA (opcode 0x27) lifts to both emitters and matches the interpreter exactly.

DAA reads AL + AF + CF and writes AL + CF/AF/ZF/SF/PF (OF undefined).  The
interpreter's op 0x27 is the single source of truth (CPU8086.daa); the VMless
emitter emits `cpu.daa()` (trivially identical); this pins the CPUless emitter's
INLINE form -- pure Python over locals -- against the interpreter over the whole
(AL, CF, AF) input space.
"""
from __future__ import annotations

from dos_re.cpu import CPU8086, CPUState, CF, AF, ZF, SF, PF
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import _translate
from dos_re.lift.cpuless import register_effects
from dos_re.lift.emit_cpuless import _flags_defined_by
from dos_re.memory import Memory
from dos_re.x86 import PARITY


def _interp(al: int, cf: bool, af: bool):
    st = CPUState()
    st.ax = 0x5A00 | al           # AH=0x5A sentinel: DAA must preserve it
    cpu = CPU8086(Memory(), st)
    cpu.set_flag(CF, cf)
    cpu.set_flag(AF, af)
    cpu.daa()
    return (cpu.s.ax, cpu.get_flag(CF), cpu.get_flag(AF),
            cpu.get_flag(ZF), cpu.get_flag(SF), cpu.get_flag(PF))


def _cpuless(al: int, cf: bool, af: bool):
    inst = decode_one(lambda o: 0x27 if o == 0 else 0x90, 0)
    lines: list[str] = []
    _translate(inst, lines, set())
    ns = {"ax": 0x5A00 | al, "cf": cf, "af": af, "_PARITY": list(PARITY)}
    exec("\n".join(lines), {}, ns)
    return (ns["ax"], ns["cf"], ns["af"], ns["zf"], ns["sf"], ns["pf"])


def test_cpuless_daa_matches_interpreter_over_all_inputs():
    for al in range(256):
        for cf in (False, True):
            for af in (False, True):
                got = _cpuless(al, cf, af)
                exp = _interp(al, cf, af)
                # normalize bools (the interpreter returns bools; exec locals too)
                assert tuple(int(x) if isinstance(x, bool) else x for x in got) == \
                       tuple(int(x) if isinstance(x, bool) else x for x in exp), \
                    f"DAA al={al:02X} cf={cf} af={af}: cpuless={got} interp={exp}"


def test_daa_abi_and_flag_tables():
    # register_effects: reads+writes AX (AL modified, AH preserved), no refusal.
    e = register_effects(decode_one(lambda o: 0x27, 0))
    assert e.refusal is None
    assert "ax" in e.reads and "ax" in e.writes
    # flag-definition table: DAA defines CF/AF/ZF/SF/PF (not OF).
    d = _flags_defined_by(decode_one(lambda o: 0x27, 0))
    assert d == frozenset({"cf", "af", "zf", "sf", "pf"})


def test_daa_preserves_ah():
    ax, *_ = _cpuless(0x9B, True, False)
    assert (ax >> 8) == 0x5A          # AH untouched by the adjust
