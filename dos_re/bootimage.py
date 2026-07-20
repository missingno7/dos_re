"""Optional destructive data-only boot-image proof generation.

This is not the canonical release packager; ``dos_re.export`` owns physical
release closure. A port's declared build-image provider may use this optional
development proof: boot the interpreted runtime, run the game's loader to its
canonical post-decompression entry, ``write_snapshot`` the state, then call
:func:`poison_snapshot_to_boot_image` here to turn that snapshot into a
data-only boot image:

* every byte the recovery IR decoded as an instruction is ZEROED (poisoned),
  except ranges the port's recovery facts declare ``code_as_data`` (bytes the
  game reads as data: self-checksums, embedded tables);
* the originating EXE path is scrubbed out of ``state.json``;
* a ``manifest.json`` records provenance (source-EXE SHA-256 + size), the
  canonical entry, the exact poison ranges, the preserved code_as_data ranges,
  and a region classification of the whole image.

The optional runtime counterpart (loading the image, arming the walls) is
:mod:`dos_re.independence`; the audit is ``tools/audit_boot_image.py``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .independence import BOOT_MANIFEST_SCHEMA


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def instruction_ranges(ir: dict) -> list[tuple[int, int]]:
    """Every decoded-instruction byte range in a recovery IR, as
    (linear_start, length).

    This is the precise recovered-CODE set: instruction bytes only, so data
    interleaved between instructions is left out (and thus preserved)."""
    ranges: list[tuple[int, int]] = []
    for key, fn in ir["functions"].items():
        seg = int(key.split(":", 1)[0], 16)
        base = seg << 4
        for block in fn["blocks"]:
            for inst in block["instructions"]:
                ip = int(inst["ip"], 16)
                length = len(inst["bytes"]) // 2
                if length:
                    ranges.append((base + ip, length))
    return ranges


def coalesce(offsets: set[int]) -> list[tuple[int, int]]:
    """Contiguous [start, end) runs over a set of byte offsets."""
    runs: list[tuple[int, int]] = []
    for off in sorted(offsets):
        if runs and off == runs[-1][1]:
            runs[-1] = (runs[-1][0], off + 1)
        else:
            runs.append((off, off + 1))
    return runs


def classify_regions(code_base: int) -> list[dict]:
    """A coarse, honest map of a real-mode image's regions -- so the manifest
    declares every region's role, not just the poisoned code."""
    return [
        {"start": 0x00000, "end": 0x00400, "kind": "dos_structure",
         "note": "real-mode interrupt vector table"},
        {"start": 0x00400, "end": 0x00500, "kind": "device_state",
         "note": "BIOS data area (video config, tick count)"},
        {"start": 0x00500, "end": code_base, "kind": "dos_structure",
         "note": "DOS/PSP low memory + program segment prefix"},
        {"start": code_base, "end": 0xA0000, "kind": "initialized_data",
         "note": "decompressed program image + heap/runtime state "
                 "(recovered code ranges within are poisoned; see poison ranges)"},
        {"start": 0xA0000, "end": 0xC0000, "kind": "device_state",
         "note": "VGA aperture / EGA planes (adapter state, restored separately)"},
        {"start": 0xC0000, "end": 0x100000, "kind": "device_state",
         "note": "upper memory / BIOS ROM area"},
    ]


def poison_snapshot_to_boot_image(
    out_dir: Path | str,
    ir_path: Path | str,
    *,
    source_exe: Path | str,
    code_seg: int,
    canonical_entry: dict,
    keep_code_as_data: list[tuple[int, int]] = (),
    poison: bool = True,
    extra_manifest: dict | None = None,
) -> dict:
    """Turn a written snapshot directory into a data-only boot image, in place.

    ``out_dir`` must already contain ``memory_1mb.bin`` + ``state.json``
    (written by ``dos_re.snapshot.write_snapshot`` at the port's canonical
    post-decompression entry).  Zeroes the recovered code, scrubs the EXE path
    from ``state.json``, writes ``manifest.json``, and returns the manifest.

    ``canonical_entry`` is the port's provenance record for the entry state
    (typically cs/ip/ss/sp + loader instruction count + a note).
    ``extra_manifest`` entries are merged into the manifest top level (e.g. a
    recovery-facts fingerprint).
    """
    out_dir = Path(out_dir)
    source_exe = Path(source_exe)
    mem_path = out_dir / "memory_1mb.bin"
    image = bytearray(mem_path.read_bytes())
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))

    inst_ranges = instruction_ranges(ir)
    poison_offsets: set[int] = set()
    for start, length in inst_ranges:
        poison_offsets.update(range(start, start + length))
    keep_offsets: set[int] = set()
    for start, length in keep_code_as_data:
        keep_offsets.update(range(start, start + length))
    # De-SMC patch slots (dos_re.lift.smc): operand fields the program patches
    # at runtime and the transformed corpus READS FROM MEMORY.  They are data
    # cells now -- poisoning them would zero the initial operand values, so
    # they are preserved automatically from the IR's smc verdicts (and land in
    # the manifest's code_as_data accounting like any declared range).
    smc_slots: list[tuple[int, int]] = []
    for rec in ir.get("functions", {}).values():
        for slot in (rec.get("smc") or {}).get("slots", ()):
            if slot.get("status") == "candidate":
                lin = (code_seg << 4) + int(slot["field_addr"], 16)
                smc_slots.append((lin, int(slot["field_size"])))
    for start, length in smc_slots:
        keep_offsets.update(range(start, start + length))
    poison_offsets -= keep_offsets

    code_bytes_present_before = sum(1 for off in poison_offsets if image[off] != 0)
    if poison:
        for off in poison_offsets:
            image[off] = 0
        mem_path.write_bytes(bytes(image))
    code_bytes_present_after = sum(1 for off in poison_offsets if image[off] != 0)

    # Scrub the originating EXE path out of state.json (the runtime must not be
    # able to find the binary through the boot image), keeping the rest.
    state_path = out_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.get("program", {}).pop("path", None)
    state.setdefault("program", {})["source"] = "<generated data-only boot image>"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    poison_runs = coalesce(poison_offsets)
    manifest = {
        "_notice": "GENERATED data-only boot image -- the strict-VMless runtime "
                   "loads THIS, never the original executable "
                   "(dos_re_2.0 section 1a').",
        "schema": BOOT_MANIFEST_SCHEMA,
        "source_exe": {
            "name": source_exe.name,
            "sha256": sha256_file(source_exe),
            "size": source_exe.stat().st_size,
        },
        "canonical_entry": dict(canonical_entry),
        "code_seg": code_seg,
        "poison": {
            "enabled": poison,
            "policy": "zero recovered-instruction bytes; preserve inter-"
                      "instruction data and declared code_as_data ranges",
            "censused_functions": len(ir["functions"]),
            "instruction_ranges": len(inst_ranges),
            "poisoned_bytes": len(poison_offsets),
            "poisoned_runs": len(poison_runs),
            "code_bytes_present_before": code_bytes_present_before,
            "code_bytes_present_after": code_bytes_present_after,
            "ranges": [[start, end - start] for start, end in poison_runs],
        },
        "code_as_data": {
            "policy": "instruction ranges the game reads as data; preserved "
                      "(not poisoned) and declared here",
            "ranges": [[start, length] for start, length in keep_code_as_data],
        },
        "smc_slots": {
            "policy": "runtime-patched operand fields the de-SMC corpus reads "
                      "from live memory (dos_re.lift.smc); preserved from the "
                      "IR's smc verdicts automatically",
            "ranges": [[start, length] for start, length in smc_slots],
        },
        "regions": classify_regions(code_seg << 4),
        "artifacts": {"memory": "memory_1mb.bin", "state": "state.json"},
    }
    manifest.update(extra_manifest or {})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                           encoding="utf-8")
    return manifest
