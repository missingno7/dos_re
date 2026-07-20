"""The synthetic "game": a hand-assembled DOS EXE with a real frame loop.

The program (one code/data segment, entry at offset 0):

    0000  xor ax,ax                 ; install our INT 09h keyboard ISR:
    0002  mov es,ax                 ;   IVT[9] = CS:0040
    0004  mov word es:[0024], 0040
    000B  mov es:[0026], cs
    0010  mov ax,0013h              ; video mode 13h (320x200x256 linear)
    0013  int 10h
    0015  mov ax,0A000h
    0018  mov es,ax                 ; ES -> framebuffer
    001A  push cs
    001B  pop ds                    ; DS = CS (counter/keystate live in-segment)
  FRAME_LOOP_TOP:
    001C  mov dx,03DAh
  WAIT_HEAD:
    001F  in al,dx                  ; wait for vertical retrace (bit 3)
    0020  test al,08h
    0022  jz WAIT_HEAD
    0024  call DRAW_FRAME
    0027  inc byte [COUNTER]
    002B  jmp FRAME_LOOP_TOP
  DRAW_FRAME:
    0030  mov al,[COUNTER]
    0033  add al,[KEYSTATE]
    0037  mov cx,0140h              ; 320 pixels
    003A  xor di,di
    003C  rep stosb                 ; paint row 0 with colour = counter+keystate
    003E  ret
  ISR:                              ; INT 09h handler
    0040  push ax
    0041  in al,60h                 ; read the scancode the hardware latched
    0043  mov cs:[KEYSTATE],al
    0047  pop ax
    0048  iret
  data:
    0050  COUNTER  db 0
    0051  KEYSTATE db 0

Per frame it paints framebuffer row 0 with colour ``(counter + keystate) & 0xFF``
— so the visible output depends on both time and input, which is exactly what
replays, snapshots, hooks, and the frame verifier need to prove things about.
"""
from __future__ import annotations

import struct
from pathlib import Path

# Program offsets (the "symbol ledger" of our synthetic game).
FRAME_LOOP_TOP = 0x001C
WAIT_HEAD = 0x001F
DRAW_FRAME = 0x0030
ISR = 0x0040
COUNTER = 0x0050
KEYSTATE = 0x0051

WIDTH = 320
FRAMEBUFFER_SEG = 0xA000

CODE = bytes.fromhex(
    "31 c0"                    # 0000 xor ax,ax
    "8e c0"                    # 0002 mov es,ax
    "26 c7 06 24 00 40 00"     # 0004 mov word es:[0024],0040 (ISR offset)
    "26 8c 0e 26 00"           # 000B mov es:[0026],cs
    "b8 13 00"                 # 0010 mov ax,0013
    "cd 10"                    # 0013 int 10
    "b8 00 a0"                 # 0015 mov ax,A000
    "8e c0"                    # 0018 mov es,ax
    "0e"                       # 001A push cs
    "1f"                       # 001B pop ds
    "ba da 03"                 # 001C mov dx,03DA        <- FRAME_LOOP_TOP
    "ec"                       # 001F in al,dx           <- WAIT_HEAD
    "a8 08"                    # 0020 test al,08
    "74 fb"                    # 0022 jz 001F
    "e8 09 00"                 # 0024 call 0030
    "fe 06 50 00"              # 0027 inc byte [0050]
    "eb ef"                    # 002B jmp 001C
    "90 90 90"                 # 002D pad
    "a0 50 00"                 # 0030 mov al,[0050]      <- DRAW_FRAME
    "02 06 51 00"              # 0033 add al,[0051]
    "b9 40 01"                 # 0037 mov cx,0140
    "31 ff"                    # 003A xor di,di
    "f3 aa"                    # 003C rep stosb
    "c3"                       # 003E ret
    "90"                       # 003F pad
    "50"                       # 0040 push ax            <- ISR
    "e4 60"                    # 0041 in al,60
    "2e a2 51 00"              # 0043 mov cs:[0051],al
    "58"                       # 0047 pop ax
    "cf"                       # 0048 iret
    "90 90 90 90 90 90 90"     # 0049 pad
    "00"                       # 0050 COUNTER
    "00"                       # 0051 KEYSTATE
)


def build_game_exe(path: Path) -> Path:
    """Write TINY.EXE — a minimal valid MZ executable containing CODE."""
    header_paragraphs = 2
    header = struct.pack(
        "<14H",
        0x5A4D,                                       # "MZ"
        (header_paragraphs * 16 + len(CODE)) % 512,   # bytes in last page
        1,                                            # pages
        0,                                            # relocations
        header_paragraphs,
        0,                                            # min extra paragraphs
        0xFFFF,                                       # max extra paragraphs
        0,                                            # initial SS (relative)
        0xFFFE,                                       # initial SP
        0,                                            # checksum
        0,                                            # initial IP
        0,                                            # initial CS (relative)
        0x1C,                                         # relocation table offset
        0,                                            # overlay number
    )
    image = bytearray(header)
    image.extend(b"\x00" * (header_paragraphs * 16 - len(header)))
    image.extend(CODE)
    path = Path(path)
    path.write_bytes(image)
    return path
