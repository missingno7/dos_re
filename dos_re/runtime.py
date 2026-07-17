from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cpu import CPU8086, CPUState
from .dos import DOSMachine
from .memory import LoadedProgram, load_mz_program
from .hooks import registry
# The Runtime shell + the EXE-FREE image constructor live in runtime_core so the
# strict-VMless import graph can reach them without reaching this module's
# load_mz_program loader edge (dos_re_2.0 §"The EXE-independence wall").
from .runtime_core import (Runtime, create_runtime_from_image, BIOS_INT9_ENTRY,
                           _BIOS_IRET_STUB, _BIOS_INT9_LINEAR,
                           install_bios_environment_hooks,
                           _init_bios_environment)


@dataclass
class PMRuntime:
    """Protected-mode (DOS/4GW LE) runtime: a flat 386 core + a DPMI/DOS host.

    The 32-bit analogue of :class:`Runtime`.  Built by :func:`create_pm_runtime`
    for Watcom/DOS4GW LE games (added for Krypton Egg)."""
    image: "object"          # dos_re.le.LEImage
    cpu: "object"            # dos_re.cpu386.CPU386
    dos: "object"            # dos_re.dos4gw.DOS4GWHost
    mem: "object"            # dos_re.cpu386.FlatMemory


def create_pm_runtime(exe_path: str | Path, *, game_root: str | Path | None = None,
                      command_tail: bytes | str = b"", ram_bytes: int = 16 * 1024 * 1024):
    """Load an MZ+LE executable into a flat 386 protected-mode runtime.

    Maps the LE image into a :class:`FlatMemory` at its own linear addresses,
    seeds the entry point/stack from the LE header, and attaches a
    :class:`DOS4GWHost` for INT 21h/31h/10h/33h.  Game knowledge (which EXE,
    command tail) stays in the adapter; this is the game-agnostic wiring.
    """
    from .le import load_le
    from .cpu386 import CPU386, FlatMemory
    from .dos4gw import DOS4GWHost, seed_low_memory

    if isinstance(command_tail, str):
        command_tail = command_tail.encode("ascii")
    exe_path = Path(exe_path)
    # Rebase the image above 1 MB like the real DOS/4G loader: the low
    # megabyte stays 1:1 (real-mode DOS memory, VGA at A0000h) and the C
    # runtime's sbrk can grow the heap above the image without ever crawling
    # into the VGA aperture (observed: KE's heap free-list reached A0000h and
    # was shredded by planar writes when loaded at the link base).
    image = load_le(exe_path, rebase=0x100000)
    mem = FlatMemory(size=ram_bytes)
    seed_low_memory(mem)   # 1:1-mapped real-mode IVT + BIOS data area
    # Place the loaded objects at their own flat linear addresses.
    mem.data[image.mem_base:image.mem_base + len(image.mem)] = image.mem
    cpu = CPU386(mem, eip=image.entry_linear, esp=image.stack_linear)
    root = Path(game_root) if game_root else exe_path.parent
    image_top = max(obj.end for obj in image.objects)
    heap_base = (image_top + 0xFFFF) & ~0xFFFF        # 64K-align above the image
    # Report a period-plausible 4 MB of free extended memory (KE's box asks
    # for 2 MB minimum), regardless of the backing store's actual size.
    dos = DOS4GWHost(mem, root, command_tail=command_tail,
                     heap_base=heap_base,
                     free_bytes=min(4 * 1024 * 1024, ram_bytes - heap_base - 0x10000))
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    # The program's AH=25 vector installs land in dos.pm_vectors; sharing the
    # dict as the CPU's IDT makes them the hardware-IRQ entry points too.
    cpu.idt = dos.pm_vectors
    cpu.pending_irq = dos.pending_irq
    dos._cpu = cpu
    return PMRuntime(image=image, cpu=cpu, dos=dos, mem=mem)


def create_runtime(
    exe_path: str | Path,
    *,
    game_root: str | Path | None = None,
    command_tail: bytes | str = b"",
) -> Runtime:
    if isinstance(command_tail, str):
        command_tail = command_tail.encode("ascii")
    exe_path = Path(exe_path)
    program = load_mz_program(exe_path, command_tail=command_tail)
    state = CPUState(
        ax=0,
        bx=0,
        cx=0,
        dx=0,
        sp=program.initial_sp,
        bp=0,
        si=0,
        di=0,
        cs=program.entry_cs,
        ip=program.entry_ip,
        ds=program.psp_segment,
        es=program.psp_segment,
        ss=program.initial_ss,
    )
    cpu = CPU8086(program.memory, state)
    root = Path(game_root) if game_root else exe_path.parent
    dos = DOSMachine(root)
    dos.seed_initial_memory_block(program.psp_segment)
    _init_bios_environment(program.memory)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    # The power-on BIOS handlers a game can vector to, in native form: the INT
    # 09h (IRQ1) keyboard ISR, so a game that installs its own INT 9 and chains
    # to the previous vector gets real BIOS scancode->buffer translation (the
    # type-ahead buffer INT 16h reads); and the dummy IRET stub, which the same
    # chaining idiom reaches on every unclaimed IRQ.
    install_bios_environment_hooks(cpu, dos)
    registry.install(cpu)
    return Runtime(program, cpu, dos)


def enable_sound_blaster(rt: Runtime, *, base: int = 0x220, irq: int = 7, dma: int = 1,
                         detection_only: bool = False):
    """Attach an emulated Sound Blaster + PIC so the program detects and uses it.

    Opt-in (an interactive front-end calls this); the deterministic demo/test path
    leaves the hardware absent so its timing is unchanged.  The front-end decides
    *how* to deliver IRQs: at batch boundaries (``pic.acknowledge`` + a forced
    ``deliver_interrupt``) to avoid interrupting the game mid-render, or inline via
    ``rt.cpu.pending_irq`` for tight detection loops.

    ``detection_only`` attaches a *detection stub* (see :class:`SoundBlaster`): the
    program detects a digital device and emits its audio commands, but no PCM is
    streamed and no playback IRQs fire — for front-ends that produce the audio with
    their own (e.g. recovered/native) engine and only need the command stream.
    """
    from .pic import PIC8259
    from .sblaster import SoundBlaster

    pic = PIC8259(imr=0x00)  # nothing masked; only IRQ0/IRQ7 are ever raised here
    sb = SoundBlaster(
        base=base, irq=irq, dma=dma,
        raise_irq=pic.raise_irq,
        read_mem=lambda a: rt.cpu.mem.data[a & 0xFFFFF],
        detection_only=detection_only,
    )
    rt.dos.pic = pic
    rt.dos.sound_blaster = sb
    # Resuming a snapshot taken mid-playback: restore the DSP/DMA programming and
    # re-arm a block IRQ so the driver's refill ISR fires and streaming continues.
    # (The PIC is left fresh — imr=0x00 is the proven cold-boot state and the game
    # re-syncs its mask via port 0x21 at runtime.)
    saved = getattr(rt.dos, "sound_blaster_snapshot", None)
    if saved:
        sb.restore_state(saved)
        sb.rearm_after_restore()
        rt.dos.sound_blaster_snapshot = None
    return sb
