"""The layered raster-effect recovery: raw journal -> classification -> plan.

A DOS game with indexed video changes DAC state WHILE the frame is displayed
so different regions of one frame use different effective colors.  The device
journals the raw ordered bus evidence (status-read runs + DAC write groups
with virtual timestamps); palette_effects classifies the most recent complete
display cycle into a renderer-facing plan -- and reports an explicit
'unresolved' rather than guessing when the evidence is insufficient.

The two real-world patterns pinned here are VGA Lemmings facts:

* briefing screen: sync -> bright generation (frame top), count 60 scan
  lines (a ~120-read run), dark generation  =>  split with an EVIDENCE-backed
  line 60;
* gameplay: sync -> level palette, then a code-timed control-bar write with
  no positional evidence  =>  split with band line None (a port fact places
  it on screen).
"""
from __future__ import annotations

from pathlib import Path

from dos_re.dos import DOSMachine
from dos_re.palette_effects import classify


class _Cpu:
    def __init__(self):
        from dos_re.memory import Memory
        self.instruction_count = 0
        self.mem = Memory()


def _write_dac(dos, cpu, index, triples, step=12):
    dos.port_write(cpu, 0x3C8, index, 8)
    for r, g, b in triples:
        cpu.instruction_count += step
        dos.port_write(cpu, 0x3C9, r, 8)
        dos.port_write(cpu, 0x3C9, g, 8)
        dos.port_write(cpu, 0x3C9, b, 8)


def _read_status(dos, cpu, reads, step=3):
    for _ in range(reads):
        cpu.instruction_count += step
        dos.port_read(cpu, 0x3DA, 8)


def _expand(v):
    return (v << 2) | (v >> 4)


# ---- device journal (raw evidence layer) ----------------------------------

def test_journal_coalesces_runs_and_groups(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    _read_status(dos, cpu, 5)
    _write_dac(dos, cpu, 0, [(1, 1, 1), (2, 2, 2)])   # contiguous -> one group
    _write_dac(dos, cpu, 20, [(3, 3, 3)])             # index jump -> new group
    _read_status(dos, cpu, 121)
    _write_dac(dos, cpu, 0, [(4, 4, 4)])              # closes the run
    kinds = [(e[0], e[1]) for e in dos.video_journal]
    assert kinds == [("st", 5), ("dac", 0), ("dac", 20), ("st", 121)]
    first_group = list(dos.video_journal)[1]
    assert len(first_group[2]) == 2                   # both triples coalesced
    # timestamps are virtual instruction counts, monotonic
    assert first_group[3] <= first_group[4]


# ---- classification (semantic layer) --------------------------------------

def _cycle_briefing(dos, cpu):
    """One briefing display cycle: sync, bright gen, count 60 lines, dark gen."""
    _read_status(dos, cpu, 3)                          # edge wait (sync)
    _write_dac(dos, cpu, 1, [(16, 16, 56)])            # bright: frame top
    _read_status(dos, cpu, 121)                        # count 60 scan lines
    _write_dac(dos, cpu, 1, [(32, 16, 8)])             # dark: from line 60


def test_briefing_pattern_classifies_as_evidence_backed_split(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    for _ in range(3):
        _cycle_briefing(dos, cpu)
    plan = dos.palette_plan()
    assert plan["kind"] == "split"
    assert plan["base"][1] == (_expand(16), _expand(16), _expand(56))
    assert len(plan["bands"]) == 1
    assert plan["bands"][0]["line"] == 60              # from the read count
    assert plan["bands"][0]["values"][1] == (_expand(32), _expand(16), _expand(8))


def test_gameplay_pattern_yields_unplaced_late_band(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    for _ in range(3):
        _read_status(dos, cpu, 2)                      # sync
        _write_dac(dos, cpu, 0, [(1, 1, 1)] * 8)       # level palette, top
        _write_dac(dos, cpu, 16, [(2, 2, 2)] * 8)
        cpu.instruction_count += 1100                  # a tick of game code
        _write_dac(dos, cpu, 16, [(9, 9, 9)] * 8)      # code-timed bar palette
    plan = dos.palette_plan()
    assert plan["kind"] == "split"
    assert plan["base"][16] == (_expand(2), _expand(2), _expand(2))
    assert len(plan["bands"]) == 1
    assert plan["bands"][0]["line"] is None            # honestly unplaced
    assert plan["bands"][0]["values"][16] == (_expand(9), _expand(9), _expand(9))


def test_no_discipline_is_static(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    _write_dac(dos, cpu, 0, [(10, 10, 10)])
    _write_dac(dos, cpu, 1, [(20, 20, 20)])
    assert dos.palette_plan()["kind"] == "static"      # no frame syncs at all
    for _ in range(3):
        _read_status(dos, cpu, 2)                      # syncs, no writes
    assert dos.palette_plan()["kind"] == "static"


def test_insufficient_evidence_is_unresolved_not_a_guess():
    # two code-timed bands in one cycle: nothing can place either -> refuse
    ev = [("st", 2, 0, 6),
          ("dac", 0, ((1, 1, 1),), 10, 20),
          ("dac", 8, ((2, 2, 2),), 5000, 5010),
          ("dac", 0, ((3, 3, 3),), 20000, 20010),
          ("st", 2, 20040, 20046)]
    plan = classify(ev)
    assert plan["kind"] == "unresolved"
    assert len(plan["bands"]) == 2


def test_classifier_is_pure_and_keeps_raw_evidence(tmp_path):
    dos = DOSMachine(Path(tmp_path))
    cpu = _Cpu()
    for _ in range(2):
        _cycle_briefing(dos, cpu)
    before = tuple(dos.video_journal)
    dos.palette_plan()
    assert tuple(dos.video_journal) == before          # journal untouched
