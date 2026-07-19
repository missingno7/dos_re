"""Real-mode INT 33h Microsoft-mouse driver + the input-demo mouse channel.

Skyroads is the first 16-bit game exercised here that actually drives INT 33h
(fn 0/1/2/3/4 -- reset, show, hide, get-position+buttons, set-position).  Until
then ``DOSMachine.int33`` was a bare "mouse absent" stub, so the game's absolute
pointer reads (fn 3) returned stale registers and the mouse did nothing.  The
driver now mirrors the proven DOS/4GW core (dos4gw.py ``_int33``) so both answer
the API identically; these tests pin that contract and the deterministic
record/replay of mouse samples.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.dos import DOSMachine
from dos_re.input_demo import MOUSE_CHANNEL, RealModeInputAdapter, mouse_payload
from dos_re.replay import ReplayEvent, ReplayPoint


class _Regs:
    ax = bx = cx = dx = 0


class _Cpu:
    def __init__(self) -> None:
        self.s = _Regs()


def _dos() -> DOSMachine:
    # These tests exercise the driver-PRESENT path, so they opt in explicitly;
    # the default (absent) is pinned separately below.
    return DOSMachine(root=Path("."), mouse_present=True)


def test_reset_reports_mouse_present_with_two_buttons() -> None:
    dos, cpu = _dos(), _Cpu()
    cpu.s.ax = 0x0000
    dos.int33(cpu)
    assert cpu.s.ax == 0xFFFF          # driver installed
    assert cpu.s.bx == 2               # two-button mouse
    assert dos.mouse_range == [0, 639, 0, 199]   # reset restores MS defaults


def test_reset_parks_the_pointer_at_the_centre_of_the_range_it_sets() -> None:
    """fn 0 restores the driver's default box [0,639]x[0,199] AND parks the
    pointer at its centre -- (320,100).  Not (160,100): that is the centre of a
    320-wide box the driver never declares, so a game reading the position
    before moving the mouse would see it off-centre."""
    dos, cpu = _dos(), _Cpu()
    cpu.s.ax = 0x0000
    dos.int33(cpu)
    assert dos.mouse_range == [0, 639, 0, 199]
    assert (dos.mouse_x, dos.mouse_y) == (320, 100)
    assert (dos.mouse_x, dos.mouse_y) == (
        (dos.mouse_range[0] + dos.mouse_range[1] + 1) // 2,
        (dos.mouse_range[2] + dos.mouse_range[3] + 1) // 2,
    )


def test_get_position_reports_the_injected_absolute_pointer() -> None:
    dos, cpu = _dos(), _Cpu()
    dos.int33(_reset(cpu))
    # front-end feeds a window-relative position; the game reads it via fn 3.
    dos.set_mouse_norm(0.75, 0.25, buttons=1)
    cpu.s.ax = 0x0003
    dos.int33(cpu)
    assert cpu.s.cx == int(0.75 * 639)   # 479
    assert cpu.s.dx == int(0.25 * 199)   # 49
    assert cpu.s.bx == 1                  # left button held


def test_get_position_clamps_to_the_active_range() -> None:
    dos, cpu = _dos(), _Cpu()
    dos.int33(_reset(cpu))
    # fn 4 set-position beyond the range; fn 3 reports the clamped value.
    cpu.s.ax, cpu.s.cx, cpu.s.dx = 0x0004, 5000, 5000
    dos.int33(cpu)
    cpu.s.ax = 0x0003
    dos.int33(cpu)
    assert (cpu.s.cx, cpu.s.dx) == (639, 199)


def test_set_range_maps_norm_into_the_program_chosen_box() -> None:
    dos, cpu = _dos(), _Cpu()
    dos.int33(_reset(cpu))
    cpu.s.ax, cpu.s.cx, cpu.s.dx = 0x0007, 0, 319       # horizontal 0..319
    dos.int33(cpu)
    cpu.s.ax, cpu.s.cx, cpu.s.dx = 0x0008, 180, 190     # vertical 180..190
    dos.int33(cpu)
    dos.set_mouse_norm(1.0, 0.0)
    cpu.s.ax = 0x0003
    dos.int33(cpu)
    assert (cpu.s.cx, cpu.s.dx) == (319, 180)


def test_unknown_service_is_a_no_op_not_a_crash() -> None:
    dos, cpu = _dos(), _Cpu()
    cpu.s.ax = 0x00FF   # a service we do not model
    dos.int33(cpu)      # must not raise (16-bit core stays lenient)


def test_mouse_replay_event_round_trips_through_json() -> None:
    e = ReplayEvent(
        ReplayPoint(7, "mouse-frame-v1"), 3, MOUSE_CHANNEL,
        mouse_payload(0.25, 0.75, 2))
    raw = e.to_json()
    back = ReplayEvent.from_json(raw)
    assert back == e


def test_replay_reinjects_a_mouse_sample_through_the_driver() -> None:
    dos, cpu = _dos(), _Cpu()
    dos.int33(_reset(cpu))

    class _RT:
        pass

    rt = _RT()
    rt.dos = dos
    event = ReplayEvent(
        ReplayPoint(0, "mouse-frame-v1"), 0, MOUSE_CHANNEL,
        mouse_payload(0.5, 0.5, 1))
    RealModeInputAdapter((event,)).apply_to_runtime(
        0, rt, deliver=lambda r, sc: None)
    cpu.s.ax = 0x0003
    dos.int33(cpu)
    assert (cpu.s.cx, cpu.s.dx, cpu.s.bx) == (int(0.5 * 639), int(0.5 * 199), 1)


def test_mouse_presence_defaults_to_absent_so_a_mouse_is_never_ambient() -> None:
    """The mouse is an explicit opt-in: by DEFAULT fn 0 reports it ABSENT
    (AX=0, BX=0) and every other service is a no-op.

    Detecting a mouse changes a game's startup control flow (it enables pointer
    control), so an ambient mouse would silently diverge every recording made
    without one -- unacceptable in a framework whose contract is byte-exact
    replay.  Front-ends opt in: the interactive viewer unconditionally, replay
    from the recording's explicit ReplayArtifact ``mouse_present`` metadata)."""
    dos, cpu = DOSMachine(root=Path(".")), _Cpu()   # default: no mouse
    cpu.s.ax = 0x0000
    dos.int33(cpu)
    assert cpu.s.ax == 0 and cpu.s.bx == 0            # "no mouse installed"
    # fn 3 leaves the caller's registers untouched (no position reported).
    cpu.s.ax, cpu.s.cx, cpu.s.dx, cpu.s.bx = 0x0003, 0x1111, 0x2222, 0x3333
    dos.int33(cpu)
    assert (cpu.s.cx, cpu.s.dx, cpu.s.bx) == (0x1111, 0x2222, 0x3333)


def _reset(cpu: _Cpu) -> _Cpu:
    cpu.s.ax = 0x0000
    return cpu
