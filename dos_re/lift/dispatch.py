"""Resolve the concrete target of a NEAR indirect transfer from live CPU state.

Near indirect jmp/call sites -- jump tables, computed function pointers, dispatch
stubs -- are unresolvable statically, but when a program runs, every target each
site takes is observable for free.  A capture probe traps such a site at its
instruction boundary and calls :func:`resolve_near_indirect_target` to record the
target the interpreter is about to take, building the ``{site: [targets]}``
evidence the CPUless promoter's EVIDENCE GATE consumes (dos_re_2.0 §6a: a
dynamic-transfer function promotes only when every observed target is
dispatchable).

Two shapes are handled, both genuine dispatches the CPUless dispatch registry
must resolve:

* **memory-indirect** (``call [bx+d]``, ``jmp cs:[bx*2+table]``) -- the target is
  the word at the computed effective address;
* **register-indirect** (``call ax``, ``jmp bx``) -- a computed function pointer
  whose target IS the register value.

The resolver is PURE: it reads an already-decoded instruction plus the register
file and memory and never mutates CPU state (unlike ``CPU8086.decode_ea``, which
fetches the displacement from the stream).  The addressing tables mirror
``decode_ea`` exactly.
"""
from __future__ import annotations

#: word registers by ModRM rm (Intel encoding order).
REG16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

#: 16-bit ModRM base/index register names by rm (mod != 3) and the default
#: segment -- BP-based addressing defaults to SS, everything else to DS (this is
#: exactly the split in ``CPU8086.decode_ea``).
_EA = (
    (("bx", "si"), "ds"), (("bx", "di"), "ds"), (("bp", "si"), "ss"), (("bp", "di"), "ss"),
    (("si",), "ds"), (("di",), "ds"), (("bp",), "ss"), (("bx",), "ds"),
)


def resolve_near_indirect_target(state, mem, inst) -> "str | None":
    """The ``'CS:IP'`` a near indirect jmp/call at ``inst`` transfers to.

    ``state`` is a register file exposing ``cs`` and the word/segment registers by
    name; ``mem`` exposes ``rw(seg, off) -> int``; ``inst`` is a decoded
    instruction (``modrm``, ``mod``, ``rm``, ``disp``, ``seg_override``).  Returns
    ``None`` when ``inst`` carries no ModRM (nothing to resolve).  Does not mutate
    ``state`` or ``mem``.
    """
    if inst.modrm is None:
        return None
    cs = state.cs & 0xFFFF
    mod, rm = inst.modrm >> 6, inst.modrm & 7
    if mod == 3:                                     # register-indirect: target = the reg value
        return f"{cs:04X}:{getattr(state, REG16[rm]) & 0xFFFF:04X}"
    seg = inst.seg_override
    if mod == 0 and rm == 6:                          # direct [disp16]
        off = (inst.disp or 0) & 0xFFFF
    else:
        regs, default_seg = _EA[rm]
        off = (sum(getattr(state, r) for r in regs) + (inst.disp or 0)) & 0xFFFF
        seg = seg or default_seg
    segval = getattr(state, seg or "ds") & 0xFFFF
    return f"{cs:04X}:{mem.rw(segval, off) & 0xFFFF:04X}"
