"""Inspect an MZ+LE (DOS/4GW-style) executable: objects, entry, fixups.

The day-0 "what am I looking at" tool for a 32-bit protected-mode title:
dumps the LE object table (bases, sizes, flags), entry point and stack, the
fixup census by source type, and — when capstone is installed — a short
disassembly at the entry point to eyeball that the image decodes as real code.

Usage:
    python tools/le_info.py assets/GAME.EXE [--rebase 0x100000] [--disasm N]

Addresses print at the link base by default; pass --rebase to see them where
the PM runtime actually loads the image (create_pm_runtime uses +0x100000).

Origin: promoted from the Krypton Egg port's load_le probe (the first LE
title), generalized to any MZ+LE input.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.le import load_le  # noqa: E402

_SRC_NAMES = {0x00: "byte", 0x02: "sel16", 0x03: "ptr16:16", 0x05: "off16",
              0x06: "ptr16:32", 0x07: "off32", 0x08: "selfrel32"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("exe", help="MZ+LE executable")
    ap.add_argument("--rebase", type=lambda v: int(v, 0), default=0,
                    help="load-address delta (runtime uses 0x100000)")
    ap.add_argument("--disasm", type=int, default=12, metavar="N",
                    help="instructions to disassemble at the entry (0 = skip)")
    args = ap.parse_args(argv)

    img = load_le(args.exe, rebase=args.rebase)
    print(f"{args.exe}: {len(img.objects)} objects, page size {img.page_size}, "
          f"{img.fixup_count} fixups, image {len(img.mem)} bytes @ 0x{img.mem_base:x}")
    print("  fixup census: " + ", ".join(
        f"{_SRC_NAMES.get(k, hex(k))}={v}" for k, v in sorted(img.fixup_census.items())))
    for obj in img.objects:
        print(f"  obj{obj.index} base=0x{obj.base:08x} vsize=0x{obj.virtual_size:x} "
              f"{'32' if obj.is_32bit else '16'}bit "
              f"{'X' if obj.executable else '-'}{'W' if obj.writable else '-'} "
              f"pages {obj.first_page}..{obj.first_page + obj.page_count - 1}")
    print(f"  entry: obj{img.entry_object}+0x{img.entry_offset:x} = 0x{img.entry_linear:x}")
    print(f"  stack: obj{img.stack_object}+0x{img.stack_offset:x} = 0x{img.stack_linear:x}")

    if args.disasm:
        try:
            import capstone
        except ImportError:
            print("  (capstone not installed — skipping entry disassembly)")
            return 0
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        off = img.entry_linear - img.mem_base
        print(f"  --- entry disassembly @0x{img.entry_linear:x} ---")
        for n, ins in enumerate(md.disasm(bytes(img.mem[off:off + 96]), img.entry_linear)):
            if n >= args.disasm:
                break
            print(f"    {ins.address:08x}: {ins.mnemonic:<8}{ins.op_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
