"""Static 16-bit x86 decoder for the lifter (lengths, control-flow class, targets).

This is deliberately NOT a second semantic model of the CPU — semantics stay
in ``dos_re.cpu`` (the oracle). The lifter only needs, per instruction: how
long it is, whether/where it branches, and a coarse mnemonic for reports and
generated-code comments. Correct-by-construction is not claimed; instead the
tool cross-checks every decoded length against the interpreter itself (patch
``cpu.fetch8`` to count bytes through one ``step()`` — the ``tools/lindis.py``
trick) and refuses to lift on any disagreement. See docs/lifting_design.md §4.

Scope: the 8086/80186 encodings the interpreter executes. Two-byte ``0F``
escapes and x87 ``ESC`` opcodes decode with correct *lengths* (so a region
scan can keep walking) but classify as UNSUPPORTED — the CFG layer refuses
functions containing them, and the census reports how often that happens.

OS-free by design: no imports from dos.py/interrupts.py (lint-enforced
extractability for a future win16_re).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Control-flow classes ("kind"):
SEQ = "seq"                  # falls through to the next instruction
JCC = "jcc"                  # conditional near branch: target + fallthrough (incl. LOOP*/JCXZ)
JMP = "jmp"                  # unconditional direct near jump
JMP_FAR = "jmp_far"          # direct far jump (static seg:off)
JMP_IND = "jmp_ind"          # indirect near/far jump — v1 lift refusal
CALL = "call"                # direct near call (static target)
CALL_FAR = "call_far"        # direct far call (static seg:off)
CALL_IND = "call_ind"        # indirect near/far call — runtime-resolvable, liftable
RET = "ret"                  # ret / ret imm16
RETF = "retf"                # retf / retf imm16
IRET = "iret"
INT = "int"                  # int n / int3 / into
HLT = "hlt"
UNSUPPORTED = "unsupported"  # decoded length may still be valid (x87); never lifted

PREFIXES = {0x26, 0x2E, 0x36, 0x3E, 0xF0, 0xF2, 0xF3}
_MAX_LEN = 15


@dataclass(frozen=True)
class Inst:
    ip: int                      # offset within the code segment
    length: int                  # total encoded bytes incl. prefixes
    kind: str
    mnemonic: str                # coarse; interpreter text supersedes it in reports
    raw: bytes
    target: int | None = None            # near branch/call target (offset)
    far_target: tuple[int, int] | None = None   # (seg, off) for direct far transfers
    int_no: int | None = None
    prefixes: tuple[int, ...] = field(default=())

    @property
    def next_ip(self) -> int:
        return (self.ip + self.length) & 0xFFFF


def _modrm_disp_len(modrm: int) -> int:
    mod = modrm >> 6
    rm = modrm & 7
    if mod == 0:
        return 2 if rm == 6 else 0
    if mod == 1:
        return 1
    if mod == 2:
        return 2
    return 0


# Opcode table: op -> (has_modrm, imm_bytes, kind, mnemonic).
# imm_bytes may be an int or "grp3b"/"grp3w" (immediate present only for /0 /1).
_T: dict[int, tuple[bool, object, str, str]] = {}


def _fill(rng, entry):
    for op in rng:
        _T[op] = entry


# 00-3F: the eight ALU groups, each 6 encodings + 2 segment/BCD slots.
for base, name in ((0x00, "add"), (0x08, "or"), (0x10, "adc"), (0x18, "sbb"),
                   (0x20, "and"), (0x28, "sub"), (0x30, "xor"), (0x38, "cmp")):
    _T[base + 0] = (True, 0, SEQ, name)      # r/m8, r8
    _T[base + 1] = (True, 0, SEQ, name)      # r/m16, r16
    _T[base + 2] = (True, 0, SEQ, name)      # r8, r/m8
    _T[base + 3] = (True, 0, SEQ, name)      # r16, r/m16
    _T[base + 4] = (False, 1, SEQ, name)     # al, imm8
    _T[base + 5] = (False, 2, SEQ, name)     # ax, imm16
for op, name in ((0x06, "push es"), (0x07, "pop es"), (0x0E, "push cs"),
                 (0x16, "push ss"), (0x17, "pop ss"), (0x1E, "push ds"),
                 (0x1F, "pop ds"), (0x27, "daa"), (0x2F, "das"),
                 (0x37, "aaa"), (0x3F, "aas")):
    _T[op] = (False, 0, SEQ, name)
_T[0x0F] = (False, 0, UNSUPPORTED, "0f-escape")  # 286+ escape; census decides if we care

_fill(range(0x40, 0x48), (False, 0, SEQ, "inc r16"))
_fill(range(0x48, 0x50), (False, 0, SEQ, "dec r16"))
_fill(range(0x50, 0x58), (False, 0, SEQ, "push r16"))
_fill(range(0x58, 0x60), (False, 0, SEQ, "pop r16"))

_T[0x60] = (False, 0, SEQ, "pusha")
_T[0x61] = (False, 0, SEQ, "popa")
_T[0x62] = (True, 0, SEQ, "bound")
_T[0x63] = (True, 0, UNSUPPORTED, "arpl")
_fill(range(0x64, 0x68), (False, 0, UNSUPPORTED, "386-prefix"))
_T[0x68] = (False, 2, SEQ, "push imm16")
_T[0x69] = (True, 2, SEQ, "imul r16,rm,imm16")
_T[0x6A] = (False, 1, SEQ, "push imm8")
_T[0x6B] = (True, 1, SEQ, "imul r16,rm,imm8")
_fill(range(0x6C, 0x70), (False, 0, SEQ, "ins/outs"))

_JCC_NAMES = ("jo jno jb jnb jz jnz jbe ja js jns jp jnp jl jge jle jg").split()
for i in range(16):
    _T[0x70 + i] = (False, 1, JCC, _JCC_NAMES[i])

_T[0x80] = (True, 1, SEQ, "grp1 rm8,imm8")
_T[0x81] = (True, 2, SEQ, "grp1 rm16,imm16")
_T[0x82] = (True, 1, SEQ, "grp1 rm8,imm8")
_T[0x83] = (True, 1, SEQ, "grp1 rm16,imm8sx")
_T[0x84] = (True, 0, SEQ, "test rm8,r8")
_T[0x85] = (True, 0, SEQ, "test rm16,r16")
_T[0x86] = (True, 0, SEQ, "xchg rm8,r8")
_T[0x87] = (True, 0, SEQ, "xchg rm16,r16")
for op in range(0x88, 0x8C):
    _T[op] = (True, 0, SEQ, "mov")
_T[0x8C] = (True, 0, SEQ, "mov rm16,sreg")
_T[0x8D] = (True, 0, SEQ, "lea")
_T[0x8E] = (True, 0, SEQ, "mov sreg,rm16")
_T[0x8F] = (True, 0, SEQ, "pop rm16")

_fill(range(0x90, 0x98), (False, 0, SEQ, "xchg ax,r16"))
_T[0x98] = (False, 0, SEQ, "cbw")
_T[0x99] = (False, 0, SEQ, "cwd")
_T[0x9A] = (False, 4, CALL_FAR, "call far")
_T[0x9B] = (False, 0, SEQ, "wait")
_T[0x9C] = (False, 0, SEQ, "pushf")
_T[0x9D] = (False, 0, SEQ, "popf")
_T[0x9E] = (False, 0, SEQ, "sahf")
_T[0x9F] = (False, 0, SEQ, "lahf")

for op in range(0xA0, 0xA4):
    _T[op] = (False, 2, SEQ, "mov moffs")
_fill(range(0xA4, 0xA8), (False, 0, SEQ, "movs/cmps"))
_T[0xA8] = (False, 1, SEQ, "test al,imm8")
_T[0xA9] = (False, 2, SEQ, "test ax,imm16")
_fill(range(0xAA, 0xB0), (False, 0, SEQ, "stos/lods/scas"))

_fill(range(0xB0, 0xB8), (False, 1, SEQ, "mov r8,imm8"))
_fill(range(0xB8, 0xC0), (False, 2, SEQ, "mov r16,imm16"))

_T[0xC0] = (True, 1, SEQ, "shift rm8,imm8")
_T[0xC1] = (True, 1, SEQ, "shift rm16,imm8")
_T[0xC2] = (False, 2, RET, "ret imm16")
_T[0xC3] = (False, 0, RET, "ret")
_T[0xC4] = (True, 0, SEQ, "les")
_T[0xC5] = (True, 0, SEQ, "lds")
_T[0xC6] = (True, 1, SEQ, "mov rm8,imm8")
_T[0xC7] = (True, 2, SEQ, "mov rm16,imm16")
_T[0xC8] = (False, 3, SEQ, "enter")
_T[0xC9] = (False, 0, SEQ, "leave")
_T[0xCA] = (False, 2, RETF, "retf imm16")
_T[0xCB] = (False, 0, RETF, "retf")
_T[0xCC] = (False, 0, INT, "int3")
_T[0xCD] = (False, 1, INT, "int")
_T[0xCE] = (False, 0, INT, "into")
_T[0xCF] = (False, 0, IRET, "iret")

for op in range(0xD0, 0xD4):
    _T[op] = (True, 0, SEQ, "shift")
_T[0xD4] = (False, 1, SEQ, "aam")
_T[0xD5] = (False, 1, SEQ, "aad")
_T[0xD6] = (False, 0, UNSUPPORTED, "salc")
_T[0xD7] = (False, 0, SEQ, "xlat")
for op in range(0xD8, 0xE0):
    _T[op] = (True, 0, UNSUPPORTED, "x87-esc")   # length decodes; never lifted

_T[0xE0] = (False, 1, JCC, "loopnz")
_T[0xE1] = (False, 1, JCC, "loopz")
_T[0xE2] = (False, 1, JCC, "loop")
_T[0xE3] = (False, 1, JCC, "jcxz")
_T[0xE4] = (False, 1, SEQ, "in al,imm8")
_T[0xE5] = (False, 1, SEQ, "in ax,imm8")
_T[0xE6] = (False, 1, SEQ, "out imm8,al")
_T[0xE7] = (False, 1, SEQ, "out imm8,ax")
_T[0xE8] = (False, 2, CALL, "call")
_T[0xE9] = (False, 2, JMP, "jmp")
_T[0xEA] = (False, 4, JMP_FAR, "jmp far")
_T[0xEB] = (False, 1, JMP, "jmp short")
_T[0xEC] = (False, 0, SEQ, "in al,dx")
_T[0xED] = (False, 0, SEQ, "in ax,dx")
_T[0xEE] = (False, 0, SEQ, "out dx,al")
_T[0xEF] = (False, 0, SEQ, "out dx,ax")

_T[0xF1] = (False, 0, UNSUPPORTED, "f1")
_T[0xF4] = (False, 0, HLT, "hlt")
_T[0xF5] = (False, 0, SEQ, "cmc")
_T[0xF6] = (True, "grp3b", SEQ, "grp3 rm8")
_T[0xF7] = (True, "grp3w", SEQ, "grp3 rm16")
for op, name in ((0xF8, "clc"), (0xF9, "stc"), (0xFA, "cli"),
                 (0xFB, "sti"), (0xFC, "cld"), (0xFD, "std")):
    _T[op] = (False, 0, SEQ, name)
_T[0xFE] = (True, 0, SEQ, "grp4")            # /0 inc /1 dec; other /r invalid (kind fixed below)
_T[0xFF] = (True, 0, SEQ, "grp5")            # kind depends on /r; fixed below


def decode_one(fetch: Callable[[int], int], ip: int) -> Inst:
    """Decode the instruction at ``ip``. ``fetch(off)`` returns the code byte
    at offset ``off`` (the caller owns segment wrap semantics)."""
    prefixes: list[int] = []
    pos = ip
    for _ in range(_MAX_LEN):
        b = fetch(pos) & 0xFF
        if b in PREFIXES:
            prefixes.append(b)
            pos = (pos + 1) & 0xFFFF
            continue
        break
    else:
        return Inst(ip, _MAX_LEN, UNSUPPORTED, "prefix-overrun",
                    bytes(fetch((ip + i) & 0xFFFF) & 0xFF for i in range(_MAX_LEN)))

    op = fetch(pos) & 0xFF
    pos = (pos + 1) & 0xFFFF
    entry = _T.get(op)
    if entry is None:
        length = (pos - ip) & 0xFFFF
        return Inst(ip, length, UNSUPPORTED, f"db 0x{op:02X}",
                    bytes(fetch((ip + i) & 0xFFFF) & 0xFF for i in range(length)),
                    prefixes=tuple(prefixes))
    has_modrm, imm, kind, mnem = entry

    modrm = None
    if has_modrm:
        modrm = fetch(pos) & 0xFF
        pos = (pos + 1 + _modrm_disp_len(modrm)) & 0xFFFF

    if imm == "grp3b":
        imm_len = 1 if modrm is not None and ((modrm >> 3) & 7) in (0, 1) else 0
    elif imm == "grp3w":
        imm_len = 2 if modrm is not None and ((modrm >> 3) & 7) in (0, 1) else 0
    else:
        imm_len = int(imm)  # type: ignore[arg-type]
    imm_pos = pos
    pos = (pos + imm_len) & 0xFFFF

    length = (pos - ip) & 0xFFFF
    raw = bytes(fetch((ip + i) & 0xFFFF) & 0xFF for i in range(length))

    target: int | None = None
    far_target: tuple[int, int] | None = None
    int_no: int | None = None

    if op == 0xFE and modrm is not None and ((modrm >> 3) & 7) not in (0, 1):
        kind, mnem = UNSUPPORTED, "grp4 invalid /r"
    elif op == 0xFF and modrm is not None:
        reg = (modrm >> 3) & 7
        kind, mnem = {
            0: (SEQ, "inc rm16"), 1: (SEQ, "dec rm16"),
            2: (CALL_IND, "call rm16"), 3: (CALL_IND, "call far rm"),
            4: (JMP_IND, "jmp rm16"), 5: (JMP_IND, "jmp far rm"),
            6: (SEQ, "push rm16"),
        }.get(reg, (UNSUPPORTED, "grp5 invalid /r"))

    if kind in (JCC, JMP, CALL):
        if imm_len == 1:
            rel = raw[-1]
            rel = rel - 0x100 if rel >= 0x80 else rel
        else:
            rel = raw[-2] | (raw[-1] << 8)
            rel = rel - 0x10000 if rel >= 0x8000 else rel
        target = (ip + length + rel) & 0xFFFF
    elif kind in (JMP_FAR, CALL_FAR):
        off = fetch(imm_pos) | (fetch((imm_pos + 1) & 0xFFFF) << 8)
        seg = fetch((imm_pos + 2) & 0xFFFF) | (fetch((imm_pos + 3) & 0xFFFF) << 8)
        far_target = (seg & 0xFFFF, off & 0xFFFF)
    elif kind == INT:
        int_no = {0xCC: 3, 0xCE: 4}.get(op, raw[-1] if imm_len else None)

    return Inst(ip, length, kind, mnem, raw, target=target, far_target=far_target,
                int_no=int_no, prefixes=tuple(prefixes))
