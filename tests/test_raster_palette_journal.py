"""The raster palette journal: mid-frame palette changes recovered generically.

A DOS game with indexed video changes DAC state WHILE the frame is displayed
so different regions of one frame use different effective colors (VGA
Lemmings: control-bar palette loaded into DAC 16..23 late in the frame, level
palette reloaded at the retrace edge).  The device model journals DAC writes
per observed display frame -- split at each 3DAh read that shows the vertical
retrace bit rising -- so a renderer can compose bands without any lift-time
analysis: the port-effect stream is already preserved byte-exactly by the
lifting pipeline on every runtime.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.dos import DOSMachine


class _Cpu:
    def __init__(self):
        from dos_re.memory import Memory
        self.instruction_count = 0
        self.mem = Memory()


def _write_dac(dos, cpu, index, triples):
    dos.port_write(cpu, 0x3C8, index, 8)
    for r, g, b in triples:
        dos.port_write(cpu, 0x3C9, r, 8)
        dos.port_write(cpu, 0x3C9, g, 8)
        dos.port_write(cpu, 0x3C9, b, 8)


def test_journal_splits_at_the_observed_retrace_edge(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    # late-frame write: the control-bar bank
    _write_dac(dos, cpu, 16, [(0, 22, 0)])
    # the game observes the retrace edge (bit3 rising across reads)
    seen = 0
    while seen < 1:
        if dos.port_read(cpu, 0x3DA, 8) & 0x08:
            seen += 1
    # post-edge write: the level bank, same DAC entry
    _write_dac(dos, cpu, 16, [(22, 22, 8)])
    # the journal must expose the LATE value where it differs from live
    split = dos.raster_split_palette()
    assert 16 in split
    assert split[16] != tuple(dos.vga_palette[16])
    # live palette carries the post-edge value
    assert tuple(dos.vga_palette[16]) == ((22 << 2) | (22 >> 4),
                                          (22 << 2) | (22 >> 4),
                                          (8 << 2) | (8 >> 4))


def test_no_discipline_means_no_split(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    # writes with no retrace observation in between: everything is one frame
    _write_dac(dos, cpu, 0, [(10, 10, 10)])
    _write_dac(dos, cpu, 1, [(20, 20, 20)])
    assert dos.raster_split_palette() == {}
    # after an edge with no further writes, late == live -> still no split
    while not (dos.port_read(cpu, 0x3DA, 8) & 0x08):
        pass
    assert dos.raster_split_palette() == {}
