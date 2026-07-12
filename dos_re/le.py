"""LE (Linear Executable) loader for DOS/4GW-bound games.

KE.EXE is a Watcom-compiled 32-bit flat-model program: an MZ real-mode stub
(the DOS/4GW loader) followed by an ``LE`` image at ``e_lfanew``.  The real
DOS/4GW extender is *bootstrap, not gameplay* (START_HERE / porting_new_game
step 1) — we do not emulate it.  Instead this loader parses the LE directly,
maps its objects into a flat 32-bit linear image, applies internal fixups, and
hands back the entry point + stack so a 386 protected-mode CPU can start at the
game's own first instruction.

Game-agnostic LE machinery (promoted here from the Krypton Egg adapter once
proven — framework grows, START_HERE "living organism").  Stdlib-only.

Formats verified empirically against KE.EXE (see probes/):
  * object page-map entry: 3-byte big-endian page number + 1 flags byte
  * fixup record: src_type, target_flags, src_offset(WORD), target...
Only the fixup/source shapes this executable actually uses are decoded; any
other shape raises loudly rather than guessing (charter: fail loud, never fake).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


# ---- LE source (fixup) types (low nibble of the record's first byte) ----------
SRC_BYTE = 0x00            # 8-bit byte offset
SRC_SELECTOR16 = 0x02      # 16-bit selector (target: selector only, NO offset)
SRC_PTR16_16 = 0x03        # 16:16 far pointer (selector + 16-bit offset)
SRC_OFFSET16 = 0x05        # 16-bit offset
SRC_PTR16_32 = 0x06        # 16:32 far pointer (selector + 32-bit offset)
SRC_OFFSET32 = 0x07        # 32-bit offset fixup (the flat-model workhorse)
SRC_SELF_REL32 = 0x08      # 32-bit self-relative offset
SRC_TYPE_MASK = 0x0F
SRC_FLAG_LIST = 0x20       # source-list form (count + N source offsets)

# Placeholder flat protected-mode selectors written by selector/pointer fixups.
# DOS/4GW maps every object into one flat 4 GB space (CS and DS/SS/ES both flat,
# base 0); the actual GDT selector values are ours to choose when the CPU is
# built.  The loader writes these so pointer fixups don't desync; the CPU host
# treats both as flat.  Refine when the 386 protected-mode core lands.
FLAT_CODE_SEL = 0x000C
FLAT_DATA_SEL = 0x0014

# ---- target flags -------------------------------------------------------------
TGT_INTERNAL = 0x00        # internal reference (the only kind in a 0-import EXE)
TGT_TYPE_MASK = 0x03
TGT_16BIT_OBJNUM = 0x40    # object number is a WORD (else BYTE)
TGT_32BIT_OFFSET = 0x10    # target offset is a DWORD (else WORD)
TGT_ADDITIVE = 0x04


@dataclass(frozen=True)
class LEObject:
    index: int              # 1-based
    virtual_size: int
    base: int               # relocation base = flat linear address
    flags: int
    first_page: int         # 1-based index into the object page map
    page_count: int

    @property
    def is_32bit(self) -> bool:
        return bool(self.flags & 0x2000)

    @property
    def executable(self) -> bool:
        return bool(self.flags & 0x0004)

    @property
    def writable(self) -> bool:
        return bool(self.flags & 0x0002)

    @property
    def end(self) -> int:
        return self.base + self.virtual_size


@dataclass
class LEImage:
    objects: list[LEObject]
    entry_object: int
    entry_offset: int       # offset within entry object
    stack_object: int
    stack_offset: int       # offset within stack object
    page_size: int
    mem: bytearray = field(repr=False)         # flat linear image
    mem_base: int = 0                          # linear address of mem[0]
    fixup_count: int = 0
    fixup_census: dict = field(default_factory=dict)   # src_type -> count

    @property
    def entry_linear(self) -> int:
        return self.objects[self.entry_object - 1].base + self.entry_offset

    @property
    def stack_linear(self) -> int:
        return self.objects[self.stack_object - 1].base + self.stack_offset

    def object_containing(self, linear: int) -> LEObject | None:
        for obj in self.objects:
            if obj.base <= linear < obj.end:
                return obj
        return None


def _u8(d, o):
    return d[o]


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def load_le(path: str | Path, *, image_limit: int | None = None) -> LEImage:
    """Parse and map an MZ+LE executable into a flat linear image.

    ``image_limit`` optionally caps the backing bytearray (default: just past
    the highest object).  The caller (the CPU/DPMI host) can grow it for the
    runtime heap/stack that DOS/4GW would normally supply.
    """
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("not an MZ executable")
    le_off = _u32(data, 0x3C)
    if data[le_off:le_off + 2] != b"LE":
        raise ValueError(f"no LE header at e_lfanew=0x{le_off:x} (got {data[le_off:le_off+2]!r})")

    h = le_off
    if _u8(data, h + 0x02) or _u8(data, h + 0x03):
        raise ValueError("big-endian LE not supported")
    page_size = _u32(data, h + 0x28)
    num_pages = _u32(data, h + 0x14)
    entry_object = _u32(data, h + 0x18)
    entry_offset = _u32(data, h + 0x1C)
    stack_object = _u32(data, h + 0x20)
    stack_offset = _u32(data, h + 0x24)
    last_page_bytes = _u32(data, h + 0x2C)
    obj_tab = h + _u32(data, h + 0x40)
    obj_cnt = _u32(data, h + 0x44)
    page_map = h + _u32(data, h + 0x48)
    fixup_page_tab = h + _u32(data, h + 0x68)
    fixup_rec_tab = h + _u32(data, h + 0x6C)
    import_cnt = _u32(data, h + 0x74)
    data_pages = _u32(data, h + 0x80)   # file offset of the page data

    if import_cnt:
        raise ValueError(f"LE has {import_cnt} imported modules; import fixups not implemented")

    # ---- object table -------------------------------------------------------
    objects: list[LEObject] = []
    for i in range(obj_cnt):
        o = obj_tab + i * 24
        objects.append(LEObject(
            index=i + 1,
            virtual_size=_u32(data, o),
            base=_u32(data, o + 4),
            flags=_u32(data, o + 8),
            first_page=_u32(data, o + 12),
            page_count=_u32(data, o + 16),
        ))

    # ---- flat image ---------------------------------------------------------
    top = max(obj.end for obj in objects)
    size = image_limit if image_limit is not None else _align(top, page_size)
    if size < top:
        raise ValueError("image_limit is below the loaded objects")
    mem = bytearray(size)

    # ---- map pages ----------------------------------------------------------
    # Page-map entry: 3-byte big-endian page number + 1 flags byte.  The page
    # number selects the physical page in the file (1-based, sequential store).
    for obj in objects:
        for local in range(obj.page_count):
            entry = page_map + (obj.first_page - 1 + local) * 4
            phys = int.from_bytes(data[entry:entry + 3], "big")
            flags = data[entry + 3]
            if flags != 0:
                raise ValueError(f"page {phys}: unsupported page flags 0x{flags:x}")
            file_off = data_pages + (phys - 1) * page_size
            nbytes = last_page_bytes if phys == num_pages else page_size
            dst = obj.base - objects[0].base  # offset into mem (mem_base = obj[0].base)
            dst += local * page_size
            mem[dst:dst + nbytes] = data[file_off:file_off + nbytes]

    mem_base = objects[0].base
    image = LEImage(
        objects=objects,
        entry_object=entry_object,
        entry_offset=entry_offset,
        stack_object=stack_object,
        stack_offset=stack_offset,
        page_size=page_size,
        mem=mem,
        mem_base=mem_base,
    )

    # ---- apply fixups -------------------------------------------------------
    _apply_fixups(data, objects, image, page_size, num_pages,
                  fixup_page_tab, fixup_rec_tab)
    return image


def _apply_fixups(data, objects, image, page_size, num_pages,
                  fixup_page_tab, fixup_rec_tab) -> None:
    mem, mem_base = image.mem, image.mem_base
    census: dict[int, int] = {}
    count = 0
    # One flat page index runs 1..num_pages across all objects, in object order.
    # page_linear[p] = flat linear address of the start of global page p.
    page_linear: dict[int, int] = {}
    p = 1
    for obj in objects:
        for local in range(obj.page_count):
            page_linear[p] = obj.base + local * page_size
            p += 1

    for page in range(1, num_pages + 1):
        start = fixup_rec_tab + _u32(data, fixup_page_tab + (page - 1) * 4)
        end = fixup_rec_tab + _u32(data, fixup_page_tab + page * 4)
        o = start
        base_linear = page_linear[page]
        while o < end:
            src_type = data[o]
            tgt_flags = data[o + 1]
            o += 2
            stype = src_type & SRC_TYPE_MASK
            # source offsets (a source-list packs N of them under one target)
            src_offsets: list[int] = []
            if src_type & SRC_FLAG_LIST:
                n = data[o]; o += 1
                for _ in range(n):
                    src_offsets.append(struct.unpack_from("<h", data, o)[0]); o += 2
            else:
                src_offsets.append(struct.unpack_from("<h", data, o)[0]); o += 2

            if (tgt_flags & TGT_TYPE_MASK) != TGT_INTERNAL:
                raise ValueError(f"page {page}: non-internal fixup (target flags 0x{tgt_flags:x})")

            # target object number
            if tgt_flags & TGT_16BIT_OBJNUM:
                obj_num = _u16(data, o); o += 2
            else:
                obj_num = data[o]; o += 1
            # target offset field — size depends on the source type; a pure
            # selector fixup has none.
            if stype == SRC_SELECTOR16:
                tgt_off = 0
            elif stype == SRC_PTR16_32:
                tgt_off = _u32(data, o); o += 4
            elif stype in (SRC_PTR16_16, SRC_OFFSET16):
                tgt_off = _u16(data, o); o += 2
            elif tgt_flags & TGT_32BIT_OFFSET:
                tgt_off = _u32(data, o); o += 4
            else:
                tgt_off = _u16(data, o); o += 2

            target = objects[obj_num - 1]
            target_linear = target.base + tgt_off
            selector = FLAT_CODE_SEL if target.executable else FLAT_DATA_SEL
            for soff in src_offsets:
                _patch(mem, mem_base, stype, base_linear + soff, target_linear,
                       selector, len(mem))
                count += 1
                census[stype] = census.get(stype, 0) + 1
    image.fixup_count = count
    image.fixup_census = census


def _patch(mem, mem_base, stype, src_linear, target_linear, selector, mem_len):
    idx = src_linear - mem_base
    # A fixup source can straddle a page boundary; the write may spill past the
    # object's virtual size into the next object's page — that is expected and
    # the flat image is contiguous, so a plain in-bounds write is correct.
    if stype == SRC_OFFSET32:
        struct.pack_into("<I", mem, idx, target_linear & 0xFFFFFFFF)
    elif stype == SRC_OFFSET16:
        struct.pack_into("<H", mem, idx, target_linear & 0xFFFF)
    elif stype == SRC_BYTE:
        mem[idx] = target_linear & 0xFF
    elif stype == SRC_SELF_REL32:
        struct.pack_into("<i", mem, idx, _s32(target_linear - (src_linear + 4)))
    elif stype == SRC_SELECTOR16:
        struct.pack_into("<H", mem, idx, selector)
    elif stype == SRC_PTR16_16:
        struct.pack_into("<H", mem, idx, target_linear & 0xFFFF)
        struct.pack_into("<H", mem, idx + 2, selector)
    elif stype == SRC_PTR16_32:
        struct.pack_into("<I", mem, idx, target_linear & 0xFFFFFFFF)
        struct.pack_into("<H", mem, idx + 4, selector)
    else:
        raise ValueError(f"unsupported LE source type 0x{stype:x} at linear 0x{src_linear:x}")


def _s32(v):
    return v & 0xFFFFFFFF


def _align(v, a):
    return (v + a - 1) & ~(a - 1)
