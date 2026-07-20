"""Byte-backed *typed views* over a DOS data segment — the state-mirror machinery.

This is the generic half of the state-mirror pattern (docs/state_mirrors.md):
recovered logic operates on a *view* (``view.wind``, ``view.slots[i].x``) and
never sees an offset; the game adapter's layout module is the ONLY place its
memory offsets are written down, as ``StructView`` subclasses built from these
descriptors.

A view holds a **backend** (the ports-and-adapters seam) and its field
descriptors address the backend in data-segment offsets:

* :class:`ByteBackend` — reads/writes straight through a flat image
  (a native game state's ``data`` or a VM ``mem.data``) at ``base + offset``.
  Byte-exact verification stays a plain memcmp of that image vs the ASM oracle.
* :class:`SegmentBackend` — same, for the game's *other* segments (level maps,
  asset banks); offsets wrap at 64 KB exactly like the 16-bit registers the
  ASM addresses them with.
* :class:`OverlayBackend` — read-through overlay: reads fall through to a base
  reader, writes ACCUMULATE a ``{offset: value}`` contract WITHOUT mutating the
  base — for whole-routine transforms returning a
  write set, verified against a golden).
* :class:`WidthContractBackend` — write-only ``{offset: (value, width)}``
  accumulator for projection passes that read only original memory and emit a
  fresh write set.

Because all backends share one interface, the SAME view (and the same recovered
logic) runs over any of them — live VM memory, a native byte image, or an
accumulating contract for a golden test.

**Width-alias convention** (the "union" answer): when the ASM reads the same
bytes at different widths, give each width its OWN named field — a different
width is a different *semantic* (a velocity word vs an anim-mirror byte). Same
storage, two meanings, two names; never a width argument at the call site.

Usage in a game adapter::

    from dos_re.state_view import ByteBackend, StructView, StructArray, U8, U16, S16, coerce_backend

    DGROUP_BASE = 0x1A0F << 4          # your game's data segment, derived from the oracle

    class PlayerView(StructView):
        x    = U16(0)
        y    = U16(2)
        xvel = S16(6)

    class GameView(StructView):        # whole-segment view: offsets ARE segment offsets
        wind  = U16(0x6BF6)
        slots = StructArray(0x4F0A, 0x12, 40, PlayerView)

        def __init__(self, source):
            super().__init__(coerce_backend(source, DGROUP_BASE), 0)

"""
from __future__ import annotations


# ---- backends -----------------------------------------------------------------------------------------------

class ByteBackend:
    """Reads/writes go straight to a flat image at ``base + offset``.

    ``base`` is the linear base of the segment the view addresses (``ds << 4``
    for the game's data segment) — derived from the loaded program, never
    hard-coded in recovered logic.
    """

    __slots__ = ("data", "base")

    def __init__(self, source, base: int = 0):
        self.data = source.data if hasattr(source, "data") else source
        self.base = base

    def rb(self, off: int) -> int:
        return self.data[self.base + (off & 0xFFFF)]

    def wb(self, off: int, v: int) -> None:
        self.data[self.base + (off & 0xFFFF)] = v & 0xFF

    def rw(self, off: int) -> int:
        a = self.base + (off & 0xFFFF)
        return self.data[a] | (self.data[a + 1] << 8)

    def ww(self, off: int, v: int) -> None:
        a = self.base + (off & 0xFFFF)
        self.data[a] = v & 0xFF
        self.data[a + 1] = (v >> 8) & 0xFF


class SegmentBackend:
    """Reads/writes through a 1 MB image at ``(seg << 4) + (offset & 0xFFFF)`` — a typed-view
    backend for the game's OTHER segments (level maps, asset banks). Offsets wrap at 64 KB
    exactly like the 16-bit registers the ASM addresses them with. The same :class:`StructView`
    machinery runs over it unchanged — only the base translation differs from :class:`ByteBackend`."""

    __slots__ = ("data", "base")

    def __init__(self, source, seg: int):
        self.data = source.data if hasattr(source, "data") else source
        self.base = (seg & 0xFFFF) << 4

    def rb(self, off: int) -> int:
        return self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF]

    def wb(self, off: int, v: int) -> None:
        self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb(off + 1) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v)
        self.wb(off + 1, v >> 8)


class OverlayBackend:
    """Read-through overlay: reads fall through to ``base_rb(offset)`` unless already written;
    writes accumulate the ``writes`` contract (``{offset: byte}``) and never touch the base.
    A contract-returning transform runs its whole-routine logic over one of these and returns
    ``overlay.writes`` as its write set — the pass stays a pure function of its inputs."""

    __slots__ = ("_base_rb", "writes")

    def __init__(self, base_rb):
        self._base_rb = base_rb          # base_rb(offset) -> the ORIGINAL byte at a segment offset
        self.writes: dict[int, int] = {}

    def rb(self, off: int) -> int:
        o = off & 0xFFFF
        return self.writes[o] if o in self.writes else self._base_rb(o)

    def wb(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v)
        self.wb((off + 1) & 0xFFFF, v >> 8)


class WidthContractBackend:
    """A write-only contract accumulator emitting ``{offset: (value, width)}`` — the
    width-tracking contract convention (vs :class:`OverlayBackend`'s
    byte-level ``{offset: value}``). Reads delegate to the implementation's own ``rb``/``rw``
    closures and do NOT see the accumulated writes — for projection passes that read
    only original memory and emit a fresh write set."""

    __slots__ = ("_rb", "_rw", "writes")

    def __init__(self, base_rb, base_rw):
        self._rb = base_rb
        self._rw = base_rw
        self.writes: dict[int, tuple[int, int]] = {}

    def rb(self, off: int) -> int:
        return self._rb(off & 0xFFFF)

    def rw(self, off: int) -> int:
        return self._rw(off & 0xFFFF)

    def wb(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = (v & 0xFF, 1)

    def ww(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = (v & 0xFFFF, 2)


# ---- field descriptors (offset RELATIVE to the view's base) -------------------------------------------------

class U16:
    """A little-endian 16-bit field."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._backend.rw(o._base + self.off)

    def __set__(self, o, v: int):
        o._backend.ww(o._base + self.off, v)


class U8:
    """An 8-bit field."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._backend.rb(o._base + self.off)

    def __set__(self, o, v: int):
        o._backend.wb(o._base + self.off, v)


class S16:
    """A little-endian *signed* 16-bit field (returns -0x8000..0x7FFF)."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        v = o._backend.rw(o._base + self.off)
        return v - 0x10000 if v & 0x8000 else v

    def __set__(self, o, v: int):
        o._backend.ww(o._base + self.off, v)


class S8:
    """An 8-bit *signed* field (returns -0x80..0x7F)."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        v = o._backend.rb(o._base + self.off)
        return v - 0x100 if v & 0x80 else v

    def __set__(self, o, v: int):
        o._backend.wb(o._base + self.off, v)


class U16Array:
    """A contiguous array of little-endian 16-bit words; ``view.field[i]`` reads/writes element ``i``."""

    def __init__(self, off: int, length: int):
        self.off = off
        self.length = length

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _U16ArrayView(o._backend, o._base + self.off, self.length)


class _U16ArrayView:
    __slots__ = ("_backend", "_base", "length")

    def __init__(self, backend, base: int, length: int):
        self._backend = backend
        self._base = base
        self.length = length

    def __getitem__(self, i: int) -> int:
        return self._backend.rw(self._base + i * 2)

    def __setitem__(self, i: int, v: int) -> None:
        self._backend.ww(self._base + i * 2, v)

    def __len__(self) -> int:
        return self.length


class StructArray:
    """A descriptor for a fixed-stride array of structs; ``view.field[i]`` returns ``struct_cls`` bound to
    ``base + i*stride`` (negative ``i`` wraps). Iterable and ``len()``-able."""

    def __init__(self, off: int, stride: int, length: int, struct_cls):
        self.off = off
        self.stride = stride
        self.length = length
        self.struct_cls = struct_cls

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _StructArrayView(o._backend, o._base + self.off, self.stride, self.length, self.struct_cls)


class _StructArrayView:
    __slots__ = ("_backend", "_base", "_stride", "length", "_cls")

    def __init__(self, backend, base: int, stride: int, length: int, cls):
        self._backend = backend
        self._base = base
        self._stride = stride
        self.length = length
        self._cls = cls

    def __getitem__(self, i: int):
        if i < 0:
            i += self.length
        return self._cls(self._backend, self._base + i * self._stride)

    def __len__(self) -> int:
        return self.length

    def __iter__(self):
        for i in range(self.length):
            yield self._cls(self._backend, self._base + i * self._stride)


# ---- view bases ---------------------------------------------------------------------------------------------

class StructView:
    """A view over ONE fixed-layout struct at a segment ``base`` offset; its field descriptors add their own
    (relative) offset to ``base``. Bind it to a backend + base — arrays hand it both."""

    __slots__ = ("_backend", "_base")

    def __init__(self, backend, base: int = 0):
        self._backend = backend
        self._base = base


def coerce_backend(source, base: int = 0):
    """A backend passes through; anything else (a native game state / VM ``mem`` / raw ``bytearray``)
    is wrapped in a :class:`ByteBackend` at ``base`` (the segment's linear base, ``ds << 4``)."""
    if isinstance(source, (ByteBackend, SegmentBackend, OverlayBackend, WidthContractBackend)):
        return source
    return ByteBackend(source, base)
