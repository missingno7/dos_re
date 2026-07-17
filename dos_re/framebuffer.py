"""The default framebuffer decoder: guest video memory -> an RGB frame.

A LEAF, on purpose.  Decoding a framebuffer needs the memory image, the video
mode and the palette -- it does not need a CPU, an interpreter, or a viewer.
This lived in ``dos_re.player``, which imports the interpreter at module level
for its VM loop; any CPU-free consumer that reached the decoder therefore
dragged the whole VM in behind it.  That is not hypothetical: a port's
rasterizer falls back here for modes it does not implement itself, so the
STANDALONE CPUless runtime -- whose entire claim is that no CPU exists -- had a
live import path to the interpreter through its own renderer.  The wall lint
could not see it (a function-local import below the roots), and the packaged
release would have carried the viewer stack it can never load.

Same rule that put CPUState in the ISA leaf (dos_re.x86): a thing every layer
needs belongs at the bottom, not inside the layer that happens to have written
it first.  ``dos_re.player`` re-exports it, so existing callers are unaffected.
"""
from __future__ import annotations

WIDTH, HEIGHT = 320, 200
PLANAR_ROW_BYTES = 40


def decode_frame_default(rt):
    """Return an HxWx3 uint8 array of the current screen (numpy imported lazily).

    Game-agnostic and therefore approximate: no pel-pan/split-screen refinements —
    it shows whatever the interpreted original draws.  BIOS text modes (0-3, 7)
    render through :mod:`dos_re.textmode` (so DOS-era text boot menus/setup
    screens are visible out of the box); ports with fancier video (CGA/Tandy)
    override ``GameFrontend.decode_frame``.
    """
    import numpy as np

    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
    from dos_re.textmode import decode_text_frame, is_text_display

    if is_text_display(rt.dos):
        return decode_text_frame(rt)

    mem = rt.cpu.mem
    pal = list(getattr(rt.dos, "vga_palette", ()) or ())
    while len(pal) < 256:
        i = len(pal)
        pal.append((i, i, i))
    pal = np.asarray(pal[:256], dtype=np.uint8)
    if mem.ega_planar or (rt.dos.video_mode & 0x7F) == 0x0D:
        start = mem.ega_display_start & 0xFFFF
        offs = (start + np.arange(HEIGHT)[:, None] * PLANAR_ROW_BYTES
                + np.arange(PLANAR_ROW_BYTES)[None, :]) & 0xFFFF
        idx = np.zeros((HEIGHT, PLANAR_ROW_BYTES, 8), dtype=np.uint8)
        for plane in range(4):
            base = EGA_APERTURE + plane * EGA_PLANE_STRIDE
            plane_bytes = np.frombuffer(mem.data, np.uint8, count=0x10000, offset=base)
            bits = np.unpackbits(plane_bytes[offs].reshape(HEIGHT, PLANAR_ROW_BYTES, 1), axis=2)
            idx |= bits << plane
        return pal[idx.reshape(HEIGHT, WIDTH)]
    # Linear VGA mode 13h (also the harmless default for anything else).
    arr = np.frombuffer(mem.data, np.uint8, count=WIDTH * HEIGHT, offset=0xA0000)
    return pal[arr.reshape(HEIGHT, WIDTH)]
