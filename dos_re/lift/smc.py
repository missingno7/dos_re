"""De-SMC analysis: classify code writes, decide which are transformable.

The scanner refuses any function whose instruction bytes are written at
runtime (``self-modifying`` / ``code-patched-at-runtime`` -- cfg.py,
irgen_core.py).  That refusal is the safety baseline and it stays: a FROZEN
lift of mutable code bytes is never acceptable.

This module is the separate, evidence-driven stage on top: it examines every
statically-visible code write against the DECODED target instruction and
decides whether the mutation can be modeled as ordinary data flow instead.

The transformation ("operand-from-memory")
------------------------------------------
For the supported classes the emitted code reads the patched field FROM THE
LIVE CODE BYTES at execution time -- ``mem.rb/rw(s.cs, field_addr)`` -- instead
of baking the snapshot's constant.  This is semantics-preserving by
construction: the real CPU decodes whatever the bytes hold at that moment, and
the transformed lift reads exactly those bytes.  Patchers need no special
handling -- their stores into the code segment are ordinary memory writes that
the transformed consumer now observes.  Patch timing, multiple patchers and
re-patching between calls are all covered by the same argument.  The patched
field cells become DATA and must survive boot-image poisoning
(``dos_re.bootimage`` preserves them from the IR automatically).

Supported mutation classes (v1)
-------------------------------
* an IMMEDIATE operand of a simple data instruction (push imm8/imm16,
  ALU acc,imm8/imm16, mov r8/r16,imm) -- observed: SkyRoads' LZS decoder
  patching its per-file bit widths, its masked-glyph blit patching a
  threshold;
* the ABSOLUTE POINTER of a direct far transfer (jmp/call far ptr16:16) --
  observed: SkyRoads' timer ISR whose chain-to-old-vector far jump is patched
  by the installer.  Absolute targets read from memory are exactly indirect
  far transfers, which the emitter already models as tail exits.

Everything else stays refused, loudly: relative branch displacements (the
patch would have to be re-expressed against the lifted block structure),
ModR/M or opcode mutation (the instruction's SHAPE changes), writes crossing
instruction boundaries, and runtime-generated code.  A function becomes a
``desmc-candidate`` only when EVERY write into it is a supported slot; one
unsupported write keeps the whole function refused.

Verification contract
---------------------
A candidate is CANDIDATE, not proven.  ``liftemit --desmc`` emits the
transformed module (banner-marked), and the ordinary differential machinery
(``liftverify`` in situ, then the end-to-end demo differential) is the
promotion gate -- run it over inputs that exercise MULTIPLE patch
configurations (e.g. every LZS file the boot decodes re-patches the widths).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cfg import FunctionScan, inst_byte_offsets
from .decode import Inst

#: Target instruction forms whose patched field is transformable, as
#: ``op -> (field_kind, field_offset_after_prefixes, field_size)``.
#: field_offset counts from the OPCODE byte (prefix length is added per inst).
_SUPPORTED_IMM_FORMS: dict[int, tuple[str, int, int]] = {}
for _op in (0x04, 0x0C, 0x14, 0x1C, 0x24, 0x2C, 0x34, 0x3C):   # ALU acc, imm8
    _SUPPORTED_IMM_FORMS[_op] = ("imm", 1, 1)
for _op in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D):   # ALU acc, imm16
    _SUPPORTED_IMM_FORMS[_op] = ("imm", 1, 2)
for _op in range(0xB0, 0xB8):                                   # mov r8, imm8
    _SUPPORTED_IMM_FORMS[_op] = ("imm", 1, 1)
for _op in range(0xB8, 0xC0):                                   # mov r16, imm16
    _SUPPORTED_IMM_FORMS[_op] = ("imm", 1, 2)
_SUPPORTED_IMM_FORMS[0x6A] = ("imm", 1, 1)                      # push imm8
_SUPPORTED_IMM_FORMS[0x68] = ("imm", 1, 2)                      # push imm16
_SUPPORTED_IMM_FORMS[0xEA] = ("far-target", 1, 4)               # jmp far ptr16:16
_SUPPORTED_IMM_FORMS[0x9A] = ("far-target", 1, 4)               # call far ptr16:16


def cs_store_width(inst: Inst) -> int:
    """Bytes written by a CS-override direct store (cfg.cs_direct_store_target).

    Every store form the detector accepts follows the x86 byte/word opcode
    pairing: an even LSB is the 8-bit form, odd the 16-bit form (A2/A3 moffs,
    88/89 mov, C6/C7 mov-imm, FE/FF inc-dec, F6/F7 not-neg, 86/87 xchg,
    00..31 ALU rm,reg, C0/C1+D0-D3 shifts, 8F pop)."""
    return 2 if (inst.op & 1) else 1


def patchable_field(inst: Inst) -> tuple[str, int, int] | None:
    """The transformable field of a supported target instruction, as
    ``(kind, code_seg_offset, size)`` -- or ``None`` if this instruction's
    mutation cannot be modeled in v1."""
    form = _SUPPORTED_IMM_FORMS.get(inst.op)
    if form is None:
        return None
    kind, off_after_prefix, size = form
    off = inst.ip + len(inst.prefixes) + off_after_prefix
    return kind, off & 0xFFFF, size


@dataclass
class PatchSlot:
    """One code write, resolved against the decoded target instruction."""
    patcher_entry: int          # entry ip of the writing function
    site: int                   # ip of the writing instruction
    write_width: int
    target_entry: int           # entry ip of the patched function
    target_ip: int              # ip of the patched instruction
    field_kind: str             # "imm" | "far-target"
    field_addr: int             # code-seg offset of the field's first byte
    field_size: int
    status: str                 # "candidate" | refusal slug
    detail: str = ""

    def report(self) -> dict:
        return {
            "patcher": f"{self.patcher_entry:04X}:{self.site:04X}",
            "write_width": self.write_width,
            "target": f"{self.target_entry:04X}:{self.target_ip:04X}",
            "field": self.field_kind,
            "field_addr": f"{self.field_addr:04X}",
            "field_size": self.field_size,
            "status": self.status,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class SmcVerdict:
    """Per patched function: transformable or not, and why."""
    entry: int
    status: str                            # "desmc-candidate" | "desmc-unsupported"
    slots: list[PatchSlot] = field(default_factory=list)

    def report(self) -> dict:
        return {"status": self.status,
                "slots": [s.report() for s in self.slots]}

    def patched_operands(self) -> dict[int, tuple[str, int, int]]:
        """``target_ip -> (kind, field_addr, field_size)`` for the emitter."""
        return {s.target_ip: (s.field_kind, s.field_addr, s.field_size)
                for s in self.slots if s.status == "candidate"}


def analyze_smc(scans: list[tuple[int, int, FunctionScan]]) -> dict[tuple[int, int], SmcVerdict]:
    """Whole-census de-SMC analysis: resolve every statically-visible code
    write against its target instruction, classify it, and produce one verdict
    per PATCHED function.  Pure and side-effect-free: the ordinary refusals
    stay in place; callers (irgen) attach the verdicts as additional evidence.
    """
    per_fn_insts: dict[tuple[int, int], dict[int, Inst]] = {}
    per_fn_bytes: dict[tuple[int, int], set[int]] = {}
    for cs, ip, scan in scans:
        per_fn_insts[(cs, ip)] = scan.insts
        per_fn_bytes[(cs, ip)] = inst_byte_offsets(scan)

    verdicts: dict[tuple[int, int], SmcVerdict] = {}
    for w_cs, w_ip, w_scan in scans:
        for site, target in w_scan.cs_store_targets:
            width = cs_store_width(w_scan.insts[site])
            for (v_cs, v_ip), owned in per_fn_bytes.items():
                if v_cs != w_cs or target not in owned:
                    continue
                verdict = verdicts.setdefault(
                    (v_cs, v_ip), SmcVerdict(v_ip, "desmc-candidate"))
                cont = next(i for i in per_fn_insts[(v_cs, v_ip)].values()
                            if i.ip <= target < i.ip + i.length)
                slot = PatchSlot(w_ip, site, width, v_ip, cont.ip,
                                 "", 0, 0, "candidate")
                fieldinfo = patchable_field(cont)
                write_range = set(range(target, target + width))
                if any(b not in range(cont.ip, cont.ip + cont.length)
                       for b in write_range):
                    slot.status = "crosses-instruction-boundary"
                    slot.detail = f"write [{target:04X}+{width}) leaves inst @{cont.ip:04X}"
                elif fieldinfo is None:
                    slot.status = "unsupported-target-form"
                    slot.detail = f"op {cont.op:02X} ({cont.mnemonic})"
                else:
                    kind, faddr, fsize = fieldinfo
                    if not write_range <= set(range(faddr, faddr + fsize)):
                        slot.status = "write-outside-operand-field"
                        slot.detail = (f"write [{target:04X}+{width}) vs "
                                       f"{kind} field [{faddr:04X}+{fsize})")
                    elif faddr < v_ip + 16:
                        # the emitted module's self_disable_if_patched guard
                        # covers the entry bytes; a patch slot inside them
                        # would trip it on every legitimate re-patch.
                        slot.status = "patched-inside-entry-signature"
                        slot.detail = f"field at {faddr:04X}, entry {v_ip:04X}"
                    else:
                        slot.field_kind, slot.field_addr, slot.field_size = kind, faddr, fsize
                verdict.slots.append(slot)

    for verdict in verdicts.values():
        if any(s.status != "candidate" for s in verdict.slots):
            verdict.status = "desmc-unsupported"
    return verdicts
