"""port_write routes each OUT to its one owner -- and the tables stay honest.

Every OUT used to be offered to all four device trackers, each of which
re-checked the port and (for a 16-bit OUT) split itself into two more calls:
12 Python calls per word write, 11 doing nothing.  It is a hot path -- EGA
planar code hits it hundreds of times per tick.

Routing is only equivalent to the fan-out because the trackers own DISJOINT
port sets, so these tests pin the two things that could silently break it:

  * the port tables in dos.py must MATCH the ports the _track_* methods
    actually compare against (re-derived here from the AST -- a hand-copied
    table drifting from its source is exactly how a device write goes missing);
  * the 16-bit asymmetry must survive: the EGA tracker consumes a word
    NATIVELY (AL=index, AH=data) at the base port, while the others split it
    into two independent byte writes.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from dos_re import dos as dos_mod
from dos_re.dos import DOSMachine


def _ports_compared_in(method_name: str) -> set:
    """The port constants a tracker actually branches on, from its source."""
    src = textwrap.dedent(inspect.getsource(getattr(DOSMachine, method_name)))
    tree = ast.parse(src)
    ports = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "port"):
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, int):
                    ports.add(c.value)
                elif isinstance(c, (ast.Tuple, ast.List)):
                    ports.update(e.value for e in c.elts
                                 if isinstance(e, ast.Constant))
    return ports


@pytest.mark.parametrize("method,table", [
    ("_track_pc_speaker", "_SPEAKER_PORTS"),
    ("_track_vga_dac_ports", "_DAC_PORTS"),
    ("_track_ega_ports", "_EGA_PORTS"),
    ("_track_adlib_ports", "_ADLIB_PORTS"),
])
def test_the_routing_table_matches_the_tracker_it_claims_to_describe(method, table):
    declared = set(getattr(dos_mod, table))
    actual = _ports_compared_in(method)
    assert actual == declared, (
        f"{table} has drifted from {method}: "
        f"missing {sorted(hex(p) for p in actual - declared)}, "
        f"stale {sorted(hex(p) for p in declared - actual)}")


def test_port_ownership_is_disjoint():
    tables = [dos_mod._SPEAKER_PORTS, dos_mod._DAC_PORTS, dos_mod._EGA_PORTS,
              dos_mod._ADLIB_PORTS]
    seen = set()
    for t in tables:
        assert not (seen & t), f"two trackers claim {sorted(seen & t)}"
        seen |= t


# ---- behaviour: routing == the historical fan-out -------------------------

class _Cpu:
    def __init__(self):
        from dos_re.memory import Memory
        self.instruction_count = 0
        self.mem = Memory()


def _mk(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    return dos, _Cpu()


def test_word_out_to_the_dac_writes_both_bytes(tmp_path):
    """The DAC tracker splits a word: index port then data port."""
    dos, cpu = _mk(tmp_path)
    dos.port_write(cpu, 0x3C8, 0x00 | (0x2A << 8), 16)   # AL->3C8, AH->3C9
    assert dos._dac_write_index == 0
    assert dos._dac_component == 1                        # 3C9 took one component
    assert dos._dac_latch == [dos_mod._dac8(0x2A)]


def test_word_out_to_the_sequencer_is_index_and_data_not_two_bytes(tmp_path):
    """The EGA asymmetry: 0F02h at 3C4h means 'map mask (02) = 0F', NOT a byte
    to 3C4 and a byte to 3C5."""
    dos, cpu = _mk(tmp_path)
    dos.port_write(cpu, 0x3C4, 0x0F02, 16)
    assert dos._seq_index == 0x02
    assert dos._seq_regs.get(0x02) == 0x0F


def test_byte_out_pair_matches_the_word_form(tmp_path):
    dos_w, cpu_w = _mk(tmp_path)
    dos_w.port_write(cpu_w, 0x3CE, 0x0805, 16)            # GC index 05 = 08
    dos_b, cpu_b = _mk(tmp_path)
    dos_b.port_write(cpu_b, 0x3CE, 0x05, 8)
    dos_b.port_write(cpu_b, 0x3CF, 0x08, 8)
    assert dos_w._gc_regs == dos_b._gc_regs
    assert dos_w._gc_index == dos_b._gc_index


def test_an_unowned_port_reaches_nobody(tmp_path):
    dos, cpu = _mk(tmp_path)
    before = (dict(dos._seq_regs), dict(dos._gc_regs), dos._dac_write_index,
              dos.opl_selected_register)
    dos.port_write(cpu, 0x0378, 0x1234, 16)               # LPT1: nobody's port
    assert (dict(dos._seq_regs), dict(dos._gc_regs), dos._dac_write_index,
            dos.opl_selected_register) == before
    assert dos.port_log[-1] == ("out", 0x0378, 0x1234, 16)   # still logged


def test_the_write_is_logged_before_routing(tmp_path):
    dos, cpu = _mk(tmp_path)
    dos.port_write(cpu, 0x3C4, 0x0F02, 16)
    assert dos.port_log[-1] == ("out", 0x3C4, 0x0F02, 16)    # one word, not two bytes


def test_adlib_word_out_sets_register_then_value(tmp_path):
    dos, cpu = _mk(tmp_path)
    dos.port_write(cpu, 0x388, 0x20 | (0x01 << 8), 16)
    assert dos.opl_selected_register == 0x20
    assert dos.opl_registers.get(0x20) == 0x01
