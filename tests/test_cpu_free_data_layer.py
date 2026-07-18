"""The CPU-free data layer: modules a no-CPU backend must be able to import.

A CPUless runtime (dos_re.lift.platform.CPUlessPlatformRuntime) runs recovered
Python with no interpreter anywhere in its import graph, and ports enforce that
with a static purity lint.  These modules are pure data/device layers, so they
must not drag ``dos_re.cpu`` in -- otherwise a CPU-free backend cannot record a
demo, persist its machine state, or decide whether a game installed its own
INT 09h, and each port ends up duplicating the logic behind the wall.

Each edge below was a real one that had to be broken:
  * ``input_demo``  -> ``.runtime`` (type hints), ``.snapshot`` (start-snapshot
    branch), ``.interrupts`` (default deliver);
  * ``keyboard``    -> held BIOS_INT9_ENTRY only in CPU-carrying ``runtime_core``;
  * ``snapshot_headless.capture_dos_state`` -> the state capture used to live
    inline in ``snapshot.write_snapshot``, which needs a CPU.
"""
from __future__ import annotations

import subprocess
import sys

CPU_FREE_MODULES = [
    "dos_re.input_demo",
    "dos_re.keyboard",
    "dos_re.snapshot_headless",
    "dos_re.framebuffer",
    "dos_re.memory",
    "dos_re.textmode",
]


def _cpu_modules_after_importing(module: str) -> list[str]:
    """Import ``module`` in a FRESH interpreter, report any dos_re.cpu pulled in.

    A subprocess is the point: within one process another test may already have
    imported the CPU, so an in-process check would pass vacuously.
    """
    code = (
        "import sys\n"
        f"import {module}\n"
        "print(','.join(sorted(m for m in sys.modules\n"
        "                      if m == 'dos_re.cpu' or m.startswith('dos_re.cpu.'))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], text=True,
                         capture_output=True, check=True)
    return [m for m in out.stdout.strip().split(",") if m]


def test_data_layer_modules_never_import_the_cpu():
    offenders = {m: pulled for m in CPU_FREE_MODULES
                 if (pulled := _cpu_modules_after_importing(m))}
    assert not offenders, f"CPU-carrying import re-introduced: {offenders}"


def test_capture_dos_state_round_trips_through_restore():
    """capture_dos_state is the inverse of _restore_dos_state.

    A field captured but not restored is silently lost on reload, so assert the
    restore path actually consumes what the capture produces.
    """
    from dos_re.dos import DOSMachine
    from dos_re.memory import Memory
    from dos_re.snapshot_headless import capture_dos_state, _restore_dos_state
    import types

    src = DOSMachine(".")
    src.video_mode = 0x13
    src.console_input_fallback = None
    src.key_queue.extend([0x1C0D, 0x3920])
    meta = capture_dos_state(src, Memory())

    dst = DOSMachine(".")
    _restore_dos_state(types.SimpleNamespace(
        dos=dst, program=types.SimpleNamespace(memory=Memory())), meta)
    assert dst.video_mode == 0x13
    assert dst.console_input_fallback is None
    assert list(dst.key_queue) == [0x1C0D, 0x3920]
