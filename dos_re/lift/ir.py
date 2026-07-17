"""Recovery IR consumers (docs/recovery_ir.md) — load + re-elaborate records.

The IR document pins every reachable instruction's bytes and length, so any
consumer can reconstruct fetch/probe from the record and let the ONE
decoder/scanner re-elaborate it — no second decode path anywhere.  Both
``tools/liftemit.py --from-ir`` and ``tools/liftlink.py --from-ir`` build
their ``FunctionScan`` objects through this module, which is what makes the
IR the single code-identity authority for the whole pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

from .cfg import FunctionScan, scan_function


def load_recovery_ir(path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    version = doc.get("ir_version")
    if version != 0:
        raise ValueError(f"unsupported recovery IR version: {version!r}")
    return doc


def code_map_from_record(rec: dict) -> tuple[dict[int, int], dict[int, int]]:
    """(byte map, length map) reconstructed from an IR function record."""
    code: dict[int, int] = {}
    lengths: dict[int, int] = {}
    for blk in rec.get("blocks", ()):
        for inst in blk["instructions"]:
            off = int(inst["ip"], 16)
            raw = bytes.fromhex(inst["bytes"])
            lengths[off] = len(raw)
            for k, b in enumerate(raw):
                code[(off + k) & 0xFFFF] = b
    return code, lengths


def scan_from_ir_record(rec: dict) -> FunctionScan:
    """Re-elaborate an IR record into a live ``FunctionScan``.

    Uses the real scanner over the record's pinned bytes; the length map
    serves as the probe, so ambiguous-length sites resolve exactly as they
    did when the IR was generated.  Raises if the record is not liftable —
    callers check ``rec["liftable"]`` first (the IR's refusals list is the
    authority for that case).  The one exception is a ``desmc-candidate``
    (dos_re.lift.smc): refused for the ORDINARY lift, but its blocks are
    pinned in the IR precisely so ``liftemit --desmc`` can re-elaborate and
    emit the transformed module from the same single source of truth.
    """
    if not rec.get("liftable") and (rec.get("smc") or {}).get("status") != "desmc-candidate":
        raise ValueError(f"IR record {rec.get('entry')} is not liftable")
    code, lengths = code_map_from_record(rec)
    cs, ip = (int(x, 16) for x in rec["entry"].split(":"))
    scan = scan_function(lambda off: code.get(off & 0xFFFF, 0x90), ip,
                         probe=lambda p: lengths.get(p & 0xFFFF))
    if not scan.liftable:
        reasons = sorted({r.reason for r in scan.refusals})
        # A desmc-candidate legitimately re-scans as self-modifying (the code
        # writes are exactly what the smc verdict modeled); every OTHER
        # refusal still fails loud.  The de-SMC emit path strips these two
        # after attaching the patch slots -- see tools/liftemit.py.
        smc_ok = ((rec.get("smc") or {}).get("status") == "desmc-candidate"
                  and set(reasons) <= {"self-modifying", "code-patched-at-runtime"})
        if not smc_ok:
            raise ValueError(f"IR record {rec['entry']} re-scan refused: "
                             + ",".join(reasons))
    return scan


def record_signature(rec: dict) -> bytes:
    return bytes.fromhex(rec["signature"])
