"""Static 32-bit x86 decoder for the lifter (lengths, control-flow class, targets).

The flat protected-mode (DOS/4GW, CPU386) counterpart of :mod:`.decode`.
Same design contract: NOT a second semantic model — semantics stay in
``dos_re.cpu386`` (the oracle).  Per instruction this reports length,
control-flow class, branch targets and operand fields; every decoded length
must be cross-checked against the interpreter itself (patch
``CPU386._fetch8`` to count bytes through one ``step()``) and any
disagreement refuses the lift.

Scope: the encodings ``CPU386`` executes — grown together with the
interpreter, enforced by the cross-check.  Defaults are 32-bit operand and
address size (the LE code objects' D bit); ``0x66``/``0x67`` flip per
instruction.  x87 ESC opcodes decode with correct lengths and classify SEQ:
unlike the 16-bit v1 (which refused them), the 32-bit emitter delegates
unknown non-transfer instructions to ``interp_one``, so an x87 line inside a
function is a fallback, not a refusal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .decode import (SEQ, JCC, JMP, JMP_FAR, JMP_IND, CALL, CALL_FAR,
                     CALL_IND, RET, RETF, IRET, INT, HLT, UNSUPPORTED)

PREFIXES = {0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65, 0x66, 0x67, 0xF0, 0xF2, 0xF3}
_SEG_PREFIX = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds", 0x64: "fs", 0x65: "gs"}
_MAX_LEN = 15


@dataclass(frozen=True)
class Inst32:
    ip: int                      # flat linear address
    length: int                  # total encoded bytes incl. prefixes
    kind: str
    mnemonic: str
    raw: bytes
    target: int | None = None    # direct branch/call target (flat linear)
    int_no: int | None = None
    prefixes: tuple[int, ...] = field(default=())
    op: int = 0                  # primary opcode byte (0x0F00|x for two-byte)
    modrm: int | None = None
    sib: int | None = None
    disp: int | None = None
    imm: int | None = None
    opsize: int = 4              # effective operand size for this instruction
    adsize: int = 4              # effective address size

    @property
    def next_ip(self) -> int:
        return (self.ip + self.length) & 0xFFFFFFFF

    @property
    def mod(self) -> int | None:
        return None if self.modrm is None else self.modrm >> 6

    @property
    def reg(self) -> int | None:
        return None if self.modrm is None else (self.modrm >> 3) & 7

    @property
    def rm(self) -> int | None:
        return None if self.modrm is None else self.modrm & 7

    @property
    def seg_override(self) -> str | None:
        for p in reversed(self.prefixes):
            if p in _SEG_PREFIX:
                return _SEG_PREFIX[p]
        return None

    @property
    def rep(self) -> int | None:
        for p in self.prefixes:
            if p in (0xF2, 0xF3):
                return p
        return None


# Immediate-width tokens: int = fixed bytes; "v" = operand size (2 or 4);
# "grp3" = imm only for modrm /0 and /1 (TEST), width per size class.
# One-byte opcode table: op -> (has_modrm, imm, kind, mnemonic, size_class)
# size_class: "b" = byte op, "v" = operand-size op, None = n/a.
_T: dict[int, tuple[bool, object, str, str]] = {}

for base, name in ((0x00, "add"), (0x08, "or"), (0x10, "adc"), (0x18, "sbb"),
                   (0x20, "and"), (0x28, "sub"), (0x30, "xor"), (0x38, "cmp")):
    _T[base + 0] = (True, 0, SEQ, name)
    _T[base + 1] = (True, 0, SEQ, name)
    _T[base + 2] = (True, 0, SEQ, name)
    _T[base + 3] = (True, 0, SEQ, name)
    _T[base + 4] = (False, 1, SEQ, name)
    _T[base + 5] = (False, "v", SEQ, name)
for op, name in ((0x06, "push es"), (0x07, "pop es"), (0x0E, "push cs"),
                 (0x16, "push ss"), (0x17, "pop ss"), (0x1E, "push ds"),
                 (0x1F, "pop ds")):
    _T[op] = (False, 0, SEQ, name)
for op in range(0x40, 0x48):
    _T[op] = (False, 0, SEQ, "inc")
for op in range(0x48, 0x50):
    _T[op] = (False, 0, SEQ, "dec")
for op in range(0x50, 0x58):
    _T[op] = (False, 0, SEQ, "push")
for op in range(0x58, 0x60):
    _T[op] = (False, 0, SEQ, "pop")
_T[0x60] = (False, 0, SEQ, "pushad")
_T[0x61] = (False, 0, SEQ, "popad")
_T[0x68] = (False, "v", SEQ, "push")
_T[0x69] = (True, "v", SEQ, "imul")
_T[0x6A] = (False, 1, SEQ, "push")
_T[0x6B] = (True, 1, SEQ, "imul")
_T[0x80] = (True, 1, SEQ, "grp1")
_T[0x81] = (True, "v", SEQ, "grp1")
_T[0x83] = (True, 1, SEQ, "grp1")
_T[0x84] = (True, 0, SEQ, "test")
_T[0x85] = (True, 0, SEQ, "test")
_T[0x86] = (True, 0, SEQ, "xchg")
_T[0x87] = (True, 0, SEQ, "xchg")
for op in (0x88, 0x89, 0x8A, 0x8B):
    _T[op] = (True, 0, SEQ, "mov")
_T[0x8C] = (True, 0, SEQ, "mov sreg")
_T[0x8D] = (True, 0, SEQ, "lea")
_T[0x8E] = (True, 0, SEQ, "mov sreg")
_T[0x8F] = (True, 0, SEQ, "pop")
for op in range(0x90, 0x98):
    _T[op] = (False, 0, SEQ, "xchg" if op != 0x90 else "nop")
_T[0x98] = (False, 0, SEQ, "cwde")
_T[0x99] = (False, 0, SEQ, "cdq")
_T[0x9B] = (False, 0, SEQ, "wait")
_T[0x9C] = (False, 0, SEQ, "pushfd")
_T[0x9D] = (False, 0, SEQ, "popfd")
_T[0x9E] = (False, 0, SEQ, "sahf")
_T[0x9F] = (False, 0, SEQ, "lahf")
for op in (0xA0, 0xA1, 0xA2, 0xA3):
    _T[op] = (False, "moffs", SEQ, "mov")
for op, name in ((0xA4, "movsb"), (0xA5, "movsd"), (0xA6, "cmpsb"), (0xA7, "cmpsd"),
                 (0xAA, "stosb"), (0xAB, "stosd"), (0xAC, "lodsb"), (0xAD, "lodsd"),
                 (0xAE, "scasb"), (0xAF, "scasd")):
    _T[op] = (False, 0, SEQ, name)
_T[0xA8] = (False, 1, SEQ, "test")
_T[0xA9] = (False, "v", SEQ, "test")
for op in range(0xB0, 0xB8):
    _T[op] = (False, 1, SEQ, "mov")
for op in range(0xB8, 0xC0):
    _T[op] = (False, "v", SEQ, "mov")
_T[0xC0] = (True, 1, SEQ, "grp2")
_T[0xC1] = (True, 1, SEQ, "grp2")
_T[0xC2] = (False, 2, RET, "ret")
_T[0xC3] = (False, 0, RET, "ret")
_T[0xC4] = (True, 0, SEQ, "les")       # load far pointer -> ES:r32
_T[0xC5] = (True, 0, SEQ, "lds")       # load far pointer -> DS:r32
_T[0xC6] = (True, 1, SEQ, "mov")
_T[0xC7] = (True, "v", SEQ, "mov")
_T[0xC8] = (False, 3, SEQ, "enter")    # iw alloc + ib level
_T[0xC9] = (False, 0, SEQ, "leave")
_T[0xCC] = (False, 0, INT, "int3")
_T[0xCD] = (False, 1, INT, "int")
_T[0xCF] = (False, 0, IRET, "iretd")
for op in (0xD0, 0xD1, 0xD2, 0xD3):
    _T[op] = (True, 0, SEQ, "grp2")
for op in range(0xD8, 0xE0):
    _T[op] = (True, 0, SEQ, "x87")     # SEQ: the 32-bit emitter falls back per line
for op, name in ((0xE0, "loopnz"), (0xE1, "loopz"), (0xE2, "loop"), (0xE3, "jecxz")):
    _T[op] = (False, 1, JCC, name)
_T[0xE4] = (False, 1, SEQ, "in")
_T[0xE5] = (False, 1, SEQ, "in")
_T[0xE6] = (False, 1, SEQ, "out")
_T[0xE7] = (False, 1, SEQ, "out")
_T[0xE8] = (False, "v", CALL, "call")
_T[0xE9] = (False, "v", JMP, "jmp")
_T[0xEA] = (False, "far", JMP_FAR, "jmp far")
_T[0xEB] = (False, 1, JMP, "jmp")
for op in (0xEC, 0xED, 0xEE, 0xEF):
    _T[op] = (False, 0, SEQ, "in/out")
_T[0xF4] = (False, 0, HLT, "hlt")
_T[0xF5] = (False, 0, SEQ, "cmc")
_T[0xF6] = (True, "grp3", SEQ, "grp3")
_T[0xF7] = (True, "grp3", SEQ, "grp3")
for op, name in ((0xF8, "clc"), (0xF9, "stc"), (0xFA, "cli"), (0xFB, "sti"),
                 (0xFC, "cld"), (0xFD, "std")):
    _T[op] = (False, 0, SEQ, name)
_T[0xFE] = (True, 0, SEQ, "grp4")
# 0xFF (grp5) is dispatched on /reg below: inc/dec/push SEQ, call/jmp indirect.
for op in range(0x70, 0x80):
    _T[op] = (False, 1, JCC, "jcc")

# Two-byte (0F xx) table.
_T2: dict[int, tuple[bool, object, str, str]] = {}
_T2[0x01] = (True, 0, SEQ, "grp7")
_T2[0x03] = (True, 0, SEQ, "lsl")      # load segment limit
_T2[0x20] = (True, 0, SEQ, "mov cr")
_T2[0x22] = (True, 0, SEQ, "mov cr")
_T2[0x31] = (False, 0, SEQ, "rdtsc")
_T2[0xA2] = (False, 0, SEQ, "cpuid")
for op2 in range(0x80, 0x90):
    _T2[op2] = (False, "v", JCC, "jcc")
for op2 in range(0x90, 0xA0):
    _T2[op2] = (True, 0, SEQ, "setcc")
for op2, name in ((0xA0, "push fs"), (0xA1, "pop fs"), (0xA8, "push gs"),
                  (0xA9, "pop gs")):
    _T2[op2] = (False, 0, SEQ, name)
for op2 in (0xA3, 0xAB, 0xB3, 0xBB):
    _T2[op2] = (True, 0, SEQ, "bt")
_T2[0xA4] = (True, 1, SEQ, "shld")
_T2[0xA5] = (True, 0, SEQ, "shld")
_T2[0xAC] = (True, 1, SEQ, "shrd")
_T2[0xAD] = (True, 0, SEQ, "shrd")
_T2[0xAF] = (True, 0, SEQ, "imul")
_T2[0xBA] = (True, 1, SEQ, "grp8")
_T2[0xBC] = (True, 0, SEQ, "bsf")
_T2[0xBD] = (True, 0, SEQ, "bsr")
for op2 in (0xB6, 0xB7, 0xBE, 0xBF):
    _T2[op2] = (True, 0, SEQ, "movzx/sx")


def _modrm_extra_len(modrm: int, adsize: int) -> tuple[int, bool]:
    """(disp bytes, has_sib) for a ModRM byte under the given address size."""
    mod = modrm >> 6
    rm = modrm & 7
    if mod == 3:
        return 0, False
    if adsize == 2:
        if mod == 0:
            return (2 if rm == 6 else 0), False
        return (1 if mod == 1 else 2), False
    has_sib = rm == 4
    if mod == 0:
        if has_sib:
            return 0, True           # base==5 handled by caller (needs SIB byte)
        return (4 if rm == 5 else 0), False
    return (1 if mod == 1 else 4), has_sib


def decode32(read, ip: int) -> Inst32:
    """Decode one instruction at flat linear ``ip``.

    ``read(addr)`` returns one code byte.  Raises ValueError past _MAX_LEN.
    """
    prefixes: list[int] = []
    opsize, adsize = 4, 4
    pos = ip
    while True:
        b = read(pos)
        if b in PREFIXES:
            prefixes.append(b)
            if b == 0x66:
                opsize = 2
            elif b == 0x67:
                adsize = 2
            pos += 1
            if pos - ip > _MAX_LEN:
                raise ValueError(f"prefix run past {_MAX_LEN} bytes at 0x{ip:X}")
            continue
        break

    op = read(pos)
    pos += 1
    two_byte = op == 0x0F
    if two_byte:
        op2 = read(pos)
        pos += 1
        entry = _T2.get(op2)
        opcode = 0x0F00 | op2
    else:
        op2 = None
        entry = _T.get(op)
        opcode = op

    # grp5 (0xFF): control-flow class depends on /reg.
    if not two_byte and op == 0xFF:
        modrm = read(pos)
        reg = (modrm >> 3) & 7
        kind = {2: CALL_IND, 3: CALL_IND, 4: JMP_IND, 5: JMP_IND}.get(reg, SEQ)
        mnem = {0: "inc", 1: "dec", 2: "call", 3: "call far", 4: "jmp",
                5: "jmp far", 6: "push"}.get(reg, "grp5?")
        entry = (True, 0, kind, mnem)

    if entry is None:
        raise ValueError(
            f"undecodable opcode 0x{op:02X}{'' if op2 is None else f' 0x{op2:02X}'} "
            f"at 0x{ip:X}")

    has_modrm, imm_spec, kind, mnemonic = entry

    modrm = sib = None
    disp = None
    if has_modrm:
        modrm = read(pos)
        pos += 1
        disp_len, has_sib = _modrm_extra_len(modrm, adsize)
        if has_sib:
            sib = read(pos)
            pos += 1
            if (sib & 7) == 5 and (modrm >> 6) == 0:
                disp_len = 4
        if disp_len:
            raw_disp = 0
            for i in range(disp_len):
                raw_disp |= read(pos + i) << (8 * i)
            pos += disp_len
            sign = 1 << (disp_len * 8 - 1)
            disp = (raw_disp & (sign - 1)) - (raw_disp & sign)

    # immediate width resolution
    if imm_spec == "v":
        imm_len = opsize
    elif imm_spec == "moffs":
        imm_len = adsize
    elif imm_spec == "far":
        imm_len = opsize + 2
    elif imm_spec == "grp3":
        reg = (modrm >> 3) & 7 if modrm is not None else 0
        if reg in (0, 1):
            imm_len = 1 if op == 0xF6 else opsize
        else:
            imm_len = 0
    else:
        imm_len = int(imm_spec)

    imm = None
    if imm_len:
        raw_imm = 0
        for i in range(imm_len):
            raw_imm |= read(pos + i) << (8 * i)
        pos += imm_len
        imm = raw_imm

    length = pos - ip
    if length > _MAX_LEN:
        raise ValueError(f"instruction longer than {_MAX_LEN} at 0x{ip:X}")

    target = None
    int_no = None
    if kind == JCC or (kind == JMP and op in (0xE9, 0xEB)) or kind == CALL:
        if op in (0xE8, 0xE9) and not two_byte:
            width = opsize
        elif two_byte:
            width = opsize
        else:
            width = 1
        sign = 1 << (width * 8 - 1)
        rel = (imm & (sign - 1)) - (imm & sign)
        target = (ip + length + rel) & 0xFFFFFFFF
    elif kind == INT and op == 0xCD:
        int_no = imm
    elif kind == INT:
        int_no = 3

    return Inst32(
        ip=ip, length=length, kind=kind, mnemonic=mnemonic,
        raw=bytes(read(ip + i) for i in range(length)),
        target=target, int_no=int_no, prefixes=tuple(prefixes),
        op=opcode, modrm=modrm, sib=sib, disp=disp, imm=imm,
        opsize=opsize, adsize=adsize,
    )
