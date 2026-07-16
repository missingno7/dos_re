"""audit_boot_image.py -- verify a GENERATED boot image is a legitimate
data-only artifact, not the executable in disguise.

Generic, game-agnostic (docs/dos_re_2.0.md section 1a').  Checks (all must
pass):

  1. no bundled executable -- no file under the image dir is an MZ/PE/COM
     image, and no file's contents hash to the recorded source EXE (rename
     defence);
  2. the manifest identifies the source binary by SHA-256 + a memory map, so
     the image is provably DERIVED from a specific EXE, not an opaque blob;
  3. every recovered code byte is poisoned -- for the exact instruction ranges
     the recovery IR decodes, the image bytes are ZERO, UNLESS the range is
     declared ``code_as_data`` (a range the game reads as data, preserved
     deliberately);
  4. the manifest's own poison accounting is internally consistent.

Any original code byte present in the image that is NOT declared code_as_data
is a failure: it would mean the EXE's code leaked into the "data-only" image.

Usage (from a port):
    python dos_re/tools/audit_boot_image.py --boot-dir generated/vmless_boot \
        --ir artifacts/lift/recovery_ir.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _instruction_ranges(ir: dict):
    for key, fn in ir["functions"].items():
        seg = int(key.split(":", 1)[0], 16)
        base = seg << 4
        for block in fn["blocks"]:
            for inst in block["instructions"]:
                length = len(inst["bytes"]) // 2
                if length:
                    yield base + int(inst["ip"], 16), length


def audit(boot_dir: Path, ir_path: Path) -> int:
    fails: list[str] = []
    manifest = json.loads((boot_dir / "manifest.json").read_text(encoding="utf-8"))
    exe_hash = manifest.get("source_exe", {}).get("sha256")

    # 1. No bundled executable anywhere in the generated tree.
    for f in boot_dir.rglob("*"):
        if not f.is_file():
            continue
        head = f.read_bytes()[:2]
        if head in (b"MZ", b"ZM"):
            fails.append(f"bundled executable image: {f.relative_to(boot_dir)} "
                         f"has an MZ header")
        if f.suffix.lower() in (".exe", ".com", ".dll"):
            fails.append(f"executable-suffixed file in image dir: {f.name}")
        if exe_hash and _sha256(f.read_bytes()) == exe_hash:
            fails.append(f"file {f.name} IS the source EXE (hash match) -- "
                         f"renaming does not launder it")

    # 2. Provenance: EXE hash + memory map present.
    if not exe_hash:
        fails.append("manifest has no source_exe.sha256 (no provenance)")
    if not manifest.get("regions"):
        fails.append("manifest has no region memory map")

    # 3/4. Poison completeness -- every decoded instruction byte is zero unless
    # declared code_as_data; manifest accounting is consistent.
    poison = manifest.get("poison", {})
    if not poison.get("enabled"):
        fails.append("poison is DISABLED -- the image carries recovered code "
                     "(rebuild without --no-poison)")
    else:
        image = (boot_dir / manifest["artifacts"]["memory"]).read_bytes()
        keep: set[int] = set()
        for start, length in manifest.get("code_as_data", {}).get("ranges", []):
            keep.update(range(start, start + length))
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        present = 0
        offenders: list[int] = []
        for start, length in _instruction_ranges(ir):
            for off in range(start, start + length):
                if off in keep:
                    continue
                if image[off] != 0:
                    present += 1
                    if len(offenders) < 8:
                        offenders.append(off)
        if present:
            fails.append(
                f"{present} recovered code byte(s) present (non-zero) and NOT "
                f"declared code_as_data -- first at "
                f"{', '.join(hex(o) for o in offenders)}")
        if poison.get("code_bytes_present_after", 0) != 0 and not keep:
            fails.append("manifest records code_bytes_present_after != 0")

    print(f"boot image audit: {boot_dir}")
    print(f"  source EXE: {manifest.get('source_exe', {}).get('name')} "
          f"sha256 {str(exe_hash)[:16]}...")
    print(f"  poison: {poison.get('poisoned_bytes')} bytes / "
          f"{poison.get('poisoned_runs')} runs, "
          f"code_as_data ranges: {len(manifest.get('code_as_data', {}).get('ranges', []))}")
    if fails:
        print(f"FAIL -- {len(fails)} problem(s):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS -- the boot image is a legitimate data-only artifact; all "
          "recovered code is poisoned or declared code_as_data.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--boot-dir", required=True)
    ap.add_argument("--ir", required=True)
    args = ap.parse_args(argv)
    return audit(Path(args.boot_dir), Path(args.ir))


if __name__ == "__main__":
    raise SystemExit(main())
