"""8042 keyboard-controller status (port 64h), PIT channel-0 direct reads
(ports 40h/43h), and INT 21h AH=0Bh -- promoted from SkyRoads's vendored
dos_re copy. All three are proven by a program that polls hardware directly
instead of going through the framework's own delivery helpers (deliver_scancode,
INT 08h): SkyRoads's intro reads the keyboard controller status before
installing its own ISR, and reads PIT channel 0 directly as a short delay
loop."""
from __future__ import annotations

from pathlib import Path

from dos_re.cpu import CPU8086, CPUState
from dos_re.dos import DOSMachine
from dos_re.interrupts import deliver_scancode
from dos_re.memory import Memory
from dos_re.runtime import Runtime


def _machine():
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFF0))
    dos = DOSMachine(root=Path("."))
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    cpu.interrupt_handler = dos.interrupt
    return cpu, dos


# --- keyboard controller status (port 64h) ---------------------------------

def test_keyboard_status_clear_until_a_scancode_is_presented():
    cpu, dos = _machine()
    assert dos.port_read(cpu, 0x64, 8) == 0x00


def test_keyboard_status_set_by_deliver_scancode_and_cleared_by_data_read():
    cpu, dos = _machine()
    dos.current_scancode = 0x1E
    dos.kbd_output_buffer_full = True
    assert dos.port_read(cpu, 0x64, 8) == 0x01
    assert dos.port_read(cpu, 0x60, 8) == 0x1E   # reading the data port...
    assert dos.port_read(cpu, 0x64, 8) == 0x00   # ...clears the status bit


def test_deliver_scancode_sets_output_buffer_full():
    mem = Memory()
    mem.load(0x1000, 0, bytes.fromhex("cf"))  # IRET at the INT 9 vector target
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFF0))
    dos = DOSMachine(root=Path("."))
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    mem.ww(0, 0x09 * 4, 0x0000)
    mem.ww(0, 0x09 * 4 + 2, 0x1000)
    rt = Runtime(program=None, cpu=cpu, dos=dos)  # type: ignore[arg-type]
    assert dos.port_read(cpu, 0x64, 8) == 0x00
    assert deliver_scancode(rt, 0x1E) is True
    assert dos.port_read(cpu, 0x64, 8) == 0x01
    assert dos.current_scancode == 0x1E


# --- PIT channel 0 direct read (ports 40h/43h) ------------------------------

def test_pit_channel0_bare_read_uses_live_value_without_disturbing_access_mode():
    cpu, dos = _machine()
    dos.pit_channel0_reload = 1000
    dos.time_source = lambda: 0.0
    lo = dos.port_read(cpu, 0x40, 8)
    assert lo == dos._pit_channel0_live_value(cpu) & 0xFF
    assert not dos._pit_channel0_read_latch  # a bare read never queues a latch


def test_pit_channel0_live_value_counts_down_and_wraps():
    cpu, dos = _machine()
    dos.pit_channel0_reload = 1000
    dos.time_source = lambda: 0.0
    assert dos._pit_channel0_live_value(cpu) == 0    # just reloaded/wrapped
    dos.time_source = lambda: 1 / dos.PIT_INPUT_HZ    # 1 tick elapsed
    assert dos._pit_channel0_live_value(cpu) == 999   # counting down from reload
    dos.time_source = lambda: 500 / dos.PIT_INPUT_HZ  # 500 ticks elapsed
    assert dos._pit_channel0_live_value(cpu) == 500
    dos.time_source = lambda: 1000 / dos.PIT_INPUT_HZ  # a full period: wraps back to 0
    assert dos._pit_channel0_live_value(cpu) == 0


def test_pit_channel0_live_value_falls_back_to_instruction_count_without_time_source():
    cpu, dos = _machine()
    dos.pit_channel0_reload = 10_000
    assert dos.time_source is None
    cpu.instruction_count = 100
    expected = 10_000 - int(100 * dos.PIT_TICKS_PER_INSTRUCTION_ESTIMATE) % 10_000
    assert dos._pit_channel0_live_value(cpu) == expected


def test_pit_channel0_latch_command_snapshots_value_for_next_two_reads():
    cpu, dos = _machine()
    dos.pit_channel0_reload = 0x1234
    dos.time_source = lambda: 0.0
    live = dos._pit_channel0_live_value(cpu)
    dos.port_write(cpu, 0x43, 0x00, 8)  # channel=0 (bits 7:6=00), access=0 (latch), mode/BCD=0
    assert dos._pit_channel0_read_latch == [live & 0xFF, (live >> 8) & 0xFF]
    # The programmed access mode (default 3) is untouched by a latch command.
    assert dos._pit_channel0_access == 3
    lo = dos.port_read(cpu, 0x40, 8)
    hi = dos.port_read(cpu, 0x40, 8)
    assert (lo, hi) == (live & 0xFF, (live >> 8) & 0xFF)
    assert not dos._pit_channel0_read_latch  # both queued bytes consumed


def test_pit_channel0_program_reload_still_works_after_latch_support_added():
    cpu, dos = _machine()
    dos.port_write(cpu, 0x43, 0x30, 8)  # channel=0, access=3 (lo/hi), mode=0
    dos.port_write(cpu, 0x40, 0x34, 8)
    dos.port_write(cpu, 0x40, 0x12, 8)
    assert dos.pit_channel0_reload == 0x1234


# --- INT 21h AH=0Bh check-stdin-status --------------------------------------

def test_check_stdin_status_reports_no_char_ready_when_queue_empty():
    cpu, dos = _machine()
    cpu.s.ax = 0x0B00
    dos.int21(cpu)
    assert cpu.s.ax & 0xFF == 0x00


def test_check_stdin_status_reports_char_ready_when_queue_has_input():
    cpu, dos = _machine()
    dos.key_queue.append(0x1E61)
    cpu.s.ax = 0x0B00
    dos.int21(cpu)
    assert cpu.s.ax & 0xFF == 0xFF
