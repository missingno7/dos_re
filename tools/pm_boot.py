"""Boot an MZ+LE (DOS/4GW) executable on the flat 386 runtime and report.

The protected-mode bring-up loop's workhorse: run the program until it stops
(fail-loud unimplemented opcode/service, program exit, or the step budget),
then report the frontier — stop reason, registers, the recent-EIP ring, hot
EIPs (sampled), and optionally a PNG of the screen.  Each run names the next
thing to implement; grow the CPU/host from what it says, re-run, repeat.

Usage:
    python tools/pm_boot.py --exe assets/GAME.EXE [--steps 20000000]
        [--keys 20]           # hex ASCII bytes seeded into the DOS key queue
        [--scancodes 39,b9]   # raw scancodes sent via the KBC after --at steps
        [--at 30000000]       # instruction count at which scancodes are sent
        [--png frame.png]     # render the final screen (chained or Mode X)

Note: EIPs print at the loaded (rebased) address — link address + 0x100000
for the default runtime.  tools/le_info.py maps between the two.

Origin: promoted from the Krypton Egg port's run_startup/render_title probes
(the first LE title), generalized to any MZ+LE input.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.runtime import create_pm_runtime          # noqa: E402
from dos_re.dos4gw import render_pm_frame              # noqa: E402
from dos_re.frame_verify import write_rgb_png          # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", required=True, help="MZ+LE executable")
    ap.add_argument("--steps", type=int, default=20_000_000)
    ap.add_argument("--keys", default="",
                    help="comma-separated hex ASCII bytes for the DOS key queue")
    ap.add_argument("--scancodes", default="",
                    help="comma-separated hex scancodes sent through the KBC")
    ap.add_argument("--at", type=int, default=0,
                    help="instruction count at which --scancodes are sent")
    ap.add_argument("--png", default="", help="write the final screen to this PNG")
    args = ap.parse_args(argv)

    rt = create_pm_runtime(args.exe)
    cpu = rt.cpu
    for tok in filter(None, args.keys.split(",")):
        rt.dos.key_queue.append(int(tok, 16))
    scancodes = [int(t, 16) for t in filter(None, args.scancodes.split(","))]
    print(f"entry eip=0x{cpu.eip:X} esp=0x{cpu.r[4]:X}")

    ring = collections.deque(maxlen=24)
    orig_step = cpu.step

    def traced_step():
        ring.append(cpu.eip)
        orig_step()
    cpu.step = traced_step

    hot = collections.Counter()
    sent = not scancodes
    chunk = 25_000
    status = "step budget exhausted"
    try:
        while cpu.instruction_count < args.steps and not cpu.halted:
            cpu.run(min(chunk, args.steps - cpu.instruction_count))
            hot[cpu.eip] += 1
            if not sent and cpu.instruction_count >= args.at:
                for sc in scancodes:
                    rt.dos.press_scancode(sc)
                sent = True
        if cpu.halted:
            status = f"program exit (code {rt.dos.exit_code})"
    except Exception as e:  # noqa: BLE001 — every fail-loud stop lands here
        status = f"{type(e).__name__}: {e}"
    print(f"\nSTOP after {cpu.instruction_count} instructions at eip=0x{cpu.eip:X}: {status}")
    print(f"  eax=0x{cpu.r[0]:08X} ebx=0x{cpu.r[3]:08X} ecx=0x{cpu.r[1]:08X} edx=0x{cpu.r[2]:08X}")
    print(f"  esi=0x{cpu.r[6]:08X} edi=0x{cpu.r[7]:08X} ebp=0x{cpu.r[5]:08X} esp=0x{cpu.r[4]:08X}")
    print("  recent eips: " + " ".join(f"0x{a:X}" for a in ring))
    print("  hot eips: " + " ".join(f"0x{a:X}x{n}" for a, n in hot.most_common(8)))
    if rt.dos.unmodeled_port_reads:
        print("  unmodeled port reads: "
              + ", ".join(f"{p:#x}({n})" for p, n in sorted(rt.dos.unmodeled_port_reads.items())))
    if args.png:
        rgb, w, h = render_pm_frame(rt.dos)
        write_rgb_png(Path(args.png), rgb, width=w, height=h)
        print(f"  wrote {args.png} (chain4={rt.dos.vga.chain4}, "
              f"display_start=0x{rt.dos.vga.display_start:x})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
