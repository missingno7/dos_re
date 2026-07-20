"""Platform-effect contract + backends for CPUless recovered functions
(M3 stage 2, dos_re_2.0.md section 4).

A recovered CPUless function computes game behaviour over ``(mem, plat, *regs)``
and receives NO CPU object.  When it must reach the machine (a port read/write,
later an interrupt) it calls the ABSTRACT ``plat`` interface.  The recovered
module imports NEITHER backend and stays CPU-carrier-free.

THE CONTRACT (duck-typed -- a recovered module names only these methods):

    plat.inp(port, width, cost) -> int          # port read  (width 1 or 2)
    plat.outp(port, value, width, cost) -> None  # port write
    plat.intr(num, regs, cost) -> regs           # INT: explicit reg bundle in/out
    plat.farcall(seg, off, regs, argbytes, cost) -> regs
        # a PLATFORM/API far-call: a static ``call far seg:off`` into a declared
        # platform-boundary segment (a Win16 import thunk, a DOS API gateway).
        # It is the far-call analogue of ``plat.intr`` -- the recovered body has
        # already written the pascal arguments and the far return frame onto the
        # emulated stack (ss:sp), so the service reads its args from there,
        # performs its effect (memory + the platform object graph), and returns
        # the register bundle it left (AX/DX + any convention clobbers) plus the
        # flags word.  ``argbytes`` is the pascal callee-cleanup (the thunk's
        # ``retf N``); the recovered body owns the stack arithmetic (SP += 4 +
        # argbytes), so the backend must NOT let its own return mechanics leak
        # the recovered body's registers/stack.
    plat.boundary(head_cs, head_ip, resume_ip, regs, cost)
        -> (regs, flags_word, extra_cost)        # boundary-head observer (t13)

``cost`` is GENERATED EXECUTION METADATA: the recovered graph's own absolute
instruction offset at the effect (``_base + _cost + in_block_offset``).  It is
the CPUless timing contract -- the backend advances ITS clock to that offset so
a time-dependent read (PIT counter, VGA retrace latch) returns the right value.
The timing is owned by the generated metadata, NOT by any VM.

TWO BACKENDS implement the same contract:

  * :class:`VMlessPlatformAdapter` -- a VERIFICATION-ONLY binding, used while
    CPUless functions execute inside the mixed VMless graph.  It binds effects
    to the live VM's ``port_reader``/``port_writer`` and sets the VM's
    ``instruction_count``.  It is NOT the runtime owner of platform behaviour.

  * :class:`CPUlessPlatformRuntime` -- an EXE-detached backend. It owns a
    device model (a :class:`DOSMachine`, which
    is pure hardware -- no instruction execution) and its OWN virtual clock.
    It imports no CPU8086, no interpreter, no lifted function.

An "unknown" effect (a port the backend does not model, later an unimplemented
interrupt) must fail loud -- never fall back to interpretation.
"""
from __future__ import annotations


# --------------------------------------------------------------------------
# Backend 1: VMless verification binding (transitional; not the runtime owner)

class VMlessPlatformAdapter:
    """Bind the ``plat`` contract to a live VM (cpu + its DOS port hooks).

    VERIFICATION ONLY: used while a CPUless function runs inside the mixed
    VMless graph, so its effects can be compared against the interpreted
    oracle.  ``entry`` is the VM's ``instruction_count`` at function entry;
    each effect's absolute virtual time is ``entry + cost``."""

    __slots__ = ("cpu", "entry")

    def __init__(self, cpu, entry: int):
        self.cpu = cpu
        self.entry = entry

    def inp(self, port: int, width: int, cost: int) -> int:
        cpu = self.cpu
        cpu.instruction_count = self.entry + cost
        if cpu.port_reader is None:
            return 0
        bits = 16 if width == 2 else 8
        return cpu.port_reader(cpu, port & 0xFFFF, bits) & (0xFFFF if width == 2 else 0xFF)

    def outp(self, port: int, value: int, width: int, cost: int) -> None:
        cpu = self.cpu
        cpu.instruction_count = self.entry + cost
        if cpu.port_writer is not None:
            bits = 16 if width == 2 else 8
            cpu.port_writer(cpu, port & 0xFFFF, value & (0xFFFF if width == 2 else 0xFF), bits)

    def intr(self, num: int, regs: dict, cost: int) -> dict:
        # Verification binding: apply the recovered body's reg bundle onto the
        # VM, run the real INT handler (the interpreted oracle's own service),
        # read the bundle back.  memory effects hit the shared VM memory.
        cpu = self.cpu
        saved = {r: getattr(cpu.s, r) for r in INT_REGS}
        saved_flags = cpu.s.flags
        cpu.instruction_count = self.entry + cost
        for r in INT_REGS:
            setattr(cpu.s, r, regs.get(r, 0) & 0xFFFF)
        cpu.s.flags = regs.get("_flags", cpu.s.flags)
        cpu.interrupt_handler(cpu, num & 0xFF)
        out = {r: getattr(cpu.s, r) & 0xFFFF for r in INT_REGS}
        out["flags"] = cpu.s.flags & 0xFFFF
        out["halted"] = bool(getattr(cpu, "halted", False))
        for r in INT_REGS:                 # restore VM regs (recovered owns them)
            setattr(cpu.s, r, saved[r])
        cpu.s.flags = saved_flags
        return out

    def farcall(self, seg: int, off: int, regs: dict, argbytes: int,
                cost: int) -> dict:
        # Verification binding: apply the recovered body's reg bundle + the
        # stack pointer (the args + far frame it already pushed live in the
        # shared VM memory), then run the real API replacement hook (the
        # interpreted oracle's own service) -- it reads its pascal args off
        # ss:sp, does its effect, and far-returns with AX/DX.  Read the bundle
        # + flags back; RESTORE the VM regs/stack/flags/cs:ip (the recovered
        # body owns them, exactly as with plat.intr): the body's own SP
        # arithmetic (SP += 4 + argbytes) is the historical pascal cleanup, so
        # the hook's ret_far pop must not leak into the recovered timeline.
        cpu = self.cpu
        key = (seg & 0xFFFF, off & 0xFFFF)
        hook = cpu.replacement_hooks.get(key)
        if hook is None:
            raise UnsupportedPlatformEffect(
                f"platform far-call {seg & 0xFFFF:04X}:{off & 0xFFFF:04X} has no "
                f"replacement hook (no API service bound at this thunk slot)")
        s = cpu.s
        saved = {r: getattr(s, r) for r in INT_REGS}
        saved_stack = (s.sp, s.ss, s.flags, s.cs, s.ip)
        base = self.entry + cost
        cpu.instruction_count = base
        for r in INT_REGS:
            setattr(s, r, regs.get(r, 0) & 0xFFFF)
        s.ss = regs.get("ss", s.ss) & 0xFFFF
        s.sp = regs.get("sp", s.sp) & 0xFFFF
        s.flags = regs.get("_flags", s.flags)
        s.cs, s.ip = seg & 0xFFFF, off & 0xFFFF
        hook(cpu)                              # the real API dispatch (ret_far)
        out = {r: getattr(s, r) & 0xFFFF for r in INT_REGS}
        out["flags"] = s.flags & 0xFFFF
        out["halted"] = bool(getattr(cpu, "halted", False))
        # VIRTUAL-TIME cost of the platform dispatch: one VM step for the thunk
        # itself (the interpreter charges the hook +1, not owns_time) PLUS any
        # nested guest execution the service re-entered (a Win16 callback -- a
        # window/enum/dialog proc -- runs guest code through the VM, and the
        # interpreter counts every instruction of it).  The recovered body owns
        # its own timeline, so the cost is DYNAMIC here (an INT service never
        # re-enters guest code, so plat.intr had no analogue).  hook() runs the
        # handler directly (not through step()), so instruction_count advanced
        # ONLY by the nested guest steps; add the +1 the thunk dispatch costs.
        out["cost"] = 1 + (cpu.instruction_count - base)
        for r in INT_REGS:                     # restore -- recovered owns them
            setattr(s, r, saved[r])
        s.sp, s.ss, s.flags, s.cs, s.ip = saved_stack
        return out

    def boundary(self, head_cs, head_ip, resume_ip, regs, cost):
        """Boundary-head observer (verification binding).  Writes the live
        bundle back to the VM so a park resumes from CURRENT state, then
        fires the VM's boundary hook (which may raise BoundaryReached).
        NOTE: parking functions are STANDALONE-ONLY in the demo graph (their
        adapters are not installed -- an unwound park would lose composed
        caller locals), so this path serves the differential harness, where
        no hook is armed and the observer is inert."""
        cpu = self.cpu
        hook = getattr(cpu, "boundary_hook", None)
        if hook is not None:
            s = cpu.s
            for r in ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di",
                      "ds", "es", "ss"):
                setattr(s, r, regs[r] & 0xFFFF)
            s.flags = (regs.get("_flags_in", 2) | 0x0002) & 0xFFFF
            s.cs = head_cs & 0xFFFF
            s.ip = resume_ip & 0xFFFF
            cpu.instruction_count = self.entry + cost
            hook(cpu, head_cs, head_ip, resume_ip)
        return regs, regs.get("_flags_in", 2), 0

    def chain_interrupt(self, vec_seg: int, vec_off: int, regs: dict,
                        cost: int) -> tuple:
        """ISR chain tail (de-SMC'd far jmp to the previous, external handler).
        Verification binding: the interpreted oracle far-jumps to the same
        target (the BIOS IRET stub in low memory), so this reads the shared VM
        memory and models the iret stub identically."""
        self.cpu.instruction_count = self.entry + cost
        return _chain_iret_stub(self.cpu.mem, vec_seg, vec_off, regs)


def make_cpu_platform(cpu):
    """VMless verification factory: a :class:`VMlessPlatformAdapter` bound to
    ``cpu`` at its current ``instruction_count`` (the function-entry time)."""
    return VMlessPlatformAdapter(cpu, cpu.instruction_count)


# --------------------------------------------------------------------------
# Backend 2: the standalone CPUless runtime (owns clock + device state)

class _ClockCarrier:
    """The ONLY state the DOS device model reads off its "cpu" argument: the
    virtual ``instruction_count`` (for the PIT), plus a dummy ``s``/``mem`` for
    the device model's diagnostic paths.  It executes NOTHING -- it is not a
    CPU carrier, just a clock + memory handle the pure device model consults."""

    __slots__ = ("instruction_count", "s", "mem")

    class _S:
        cs = 0
        ip = 0

    def __init__(self, mem):
        self.instruction_count = 0
        self.s = _ClockCarrier._S()
        self.mem = mem


class UnsupportedPlatformEffect(RuntimeError):
    """A reached platform effect the CPUless runtime does not implement.  Fail
    loud with a witness; never fall back to interpretation."""


def _chain_iret_stub(mem, vec_seg: int, vec_off: int, regs: dict) -> tuple:
    """Model a recovered ISR chaining (``jmp far``) to the PREVIOUS handler of
    its vector -- an EXTERNAL handler outside the recovered corpus.

    In the EXE-free boot image the default/uninstalled interrupt vectors point
    at the BIOS IRET stub ``seed_low_memory`` wrote (a bare ``0xCF``), so the
    chain is a NO-OP: the stub's iret simply ends the interrupt, leaving the
    register bundle and flags as the recovered body left them.  This is
    VERIFIED, not assumed -- the target's first byte is read from live memory
    and MUST be ``iret``.  Any other target is an unmodelled external handler:
    fail loud (the hard wall).  Returns the ``(regs, compat)`` pair the emitter
    expects from a chain, same shape as the recovered-handler dispatch."""
    op = mem.rb(vec_seg & 0xFFFF, vec_off & 0xFFFF)
    if op != 0xCF:
        raise UnsupportedPlatformEffect(
            f"ISR chain to {vec_seg & 0xFFFF:04X}:{vec_off & 0xFFFF:04X} is not "
            f"the BIOS IRET stub (first byte {op:02X}): no external handler is "
            f"modelled by the CPUless runtime")
    return dict(regs), {"flags": regs.get("_flags_in", 2) & 0xFFFF,
                        "fmask": 0, "cost": 0}


#: registers an INT service reads/writes as an explicit bundle (no sp/ss/cs:
#: the framework's native INT handlers model the SERVICE, not the int/iret
#: stack mechanism, and touch only the general + buffer-segment registers).
INT_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")


class _IntCarrier:
    """A register + memory carrier the pure DOS service handlers manipulate.
    It executes NOTHING -- not a CPU carrier, just the explicit INT reg bundle
    (dos_re_2.0 section 4: DOS services are platform adapters).  ``set_flag``
    and ``halted`` are the only handler hooks beyond ``s``/``mem``."""

    class _Regs:
        __slots__ = INT_REGS + ("sp", "ss", "cs", "ip", "flags")

        def __init__(self):
            for r in self.__slots__:
                setattr(self, r, 0)

        def snapshot(self):
            return {r: getattr(self, r) for r in self.__slots__}

    def __init__(self, mem):
        self.mem = mem
        self.s = _IntCarrier._Regs()
        self.halted = False
        self.instruction_count = 0

    def set_flag(self, flag: int, value) -> None:
        self.s.flags = (self.s.flags | flag) if value else (self.s.flags & ~flag)


def _run_int(dos, carrier, num, regs, cost, flags_in):
    """Apply the reg bundle, run the DOS service, return the updated bundle +
    flags.  Shared by both backends (identical service semantics; only the
    carrier/clock ownership differs)."""
    carrier.instruction_count = cost
    s = carrier.s
    for r in INT_REGS:
        setattr(s, r, regs.get(r, 0) & 0xFFFF)
    s.flags = flags_in
    carrier.halted = False
    dos.interrupt(carrier, num & 0xFF)
    out = {r: getattr(s, r) & 0xFFFF for r in INT_REGS}
    out["flags"] = s.flags & 0xFFFF
    out["halted"] = carrier.halted
    return out


class CPUlessPlatformRuntime:
    """Standalone platform backend for an EXE-detached execution plan.

    Owns the historical memory image + a device model (:class:`DOSMachine`,
    pure hardware) + its own virtual clock.  Implements the ``plat`` contract
    directly, with NO CPU8086, NO interpreter, NO lifted function.  A recovered
    ROOT call is wrapped by :meth:`call`, which snapshots the entry clock,
    routes the body's effects through this runtime, and advances the clock by
    the body's reported cost."""

    def __init__(self, mem, game_root, *, dos=None):
        self.mem = mem
        self.clock = 0
        self._entry = 0
        self._carrier = _ClockCarrier(mem)
        #: the standalone SCHEDULER seam: the selected composition installs a callback
        #: (head_cs, head_ip, resume_ip, regs, abs_cost) -> (regs, flags,
        #: extra_cost) that counts boundary-head passes and, on quota, PARKS
        #: in-line: applies demo inputs, delivers timer IRQs through the
        #: recovered HANDLERS, and returns the post-IRQ state.  Without a
        #: callback the observer is inert (free-running).
        self.boundary_cb = None
        #: the BLOCKING-READ seam: a console read (INT 21h AH=01/07/08, INT 16h)
        #: with an empty type-ahead buffer must WAIT for input.  A flat CPU
        #: rewinds its IP and re-runs the instruction next frame; the CPUless
        #: backend cannot rewind a Python call stack, so a driver installs a
        #: callback that advances ONE frame in place (capture + demo input +
        #: timer IRQs) so awaited input can arrive, after which ``intr`` retries
        #: the read.  Without a callback a blocking read fails loud (the runner
        #: has no input source), never synthesising a phantom key.
        self.blocking_read_cb = None
        if dos is not None:
            self.dos = dos                 # reuse a prepared device model
        else:
            from pathlib import Path
            from dos_re.dos import DOSMachine
            self.dos = DOSMachine(Path(game_root))   # root must be a Path
                                                     # (file services join it)

    # -- the plat contract ------------------------------------------------

    def inp(self, port: int, width: int, cost: int) -> int:
        self._carrier.instruction_count = self._entry + cost
        bits = 16 if width == 2 else 8
        try:
            v = self.dos.port_read(self._carrier, port & 0xFFFF, bits)
        except Exception as e:  # noqa: BLE001 -- unmodelled port: fail loud
            raise UnsupportedPlatformEffect(
                f"port read {port & 0xFFFF:04X} (width {width}) not implemented "
                f"by the CPUless runtime: {e}") from e
        return v & (0xFFFF if width == 2 else 0xFF)

    def outp(self, port: int, value: int, width: int, cost: int) -> None:
        self._carrier.instruction_count = self._entry + cost
        bits = 16 if width == 2 else 8
        try:
            self.dos.port_write(self._carrier, port & 0xFFFF,
                                value & (0xFFFF if width == 2 else 0xFF), bits)
        except Exception as e:  # noqa: BLE001
            raise UnsupportedPlatformEffect(
                f"port write {port & 0xFFFF:04X} (width {width}) not implemented "
                f"by the CPUless runtime: {e}") from e

    def intr(self, num: int, regs: dict, cost: int) -> dict:
        from dos_re.x86 import HaltExecution
        from dos_re.dos import ConsoleInputWouldBlock
        if not hasattr(self, "_int_carrier"):
            self._int_carrier = _IntCarrier(self.mem)
        while True:
            try:
                return _run_int(self.dos, self._int_carrier, num, regs,
                                self._entry + cost, regs.get("_flags", 0))
            except ConsoleInputWouldBlock:
                # A console read found the type-ahead buffer empty.  On a flat
                # CPU the DOS handler rewinds IP and the read re-runs next frame;
                # here the driver advances one frame in place (delivering demo
                # input + timer IRQs) so the awaited key can arrive, then we
                # retry the SAME read.  No driver -> fail loud (never a phantom).
                if self.blocking_read_cb is None:
                    raise UnsupportedPlatformEffect(
                        f"INT {num & 0xFF:02X} console read would block with no "
                        f"input source: install blocking_read_cb or set "
                        f"dos.console_input_fallback") from None
                self.blocking_read_cb(regs)
                continue
            except UnsupportedPlatformEffect:
                raise
            except HaltExecution:
                raise           # the program ended (int 21/4C): a real exit
            except Exception as e:  # noqa: BLE001 -- unset vector / unmodelled INT
                raise UnsupportedPlatformEffect(
                    f"INT {num & 0xFF:02X} not implemented by the CPUless runtime "
                    f"(unset vector or game-installed handler): {e}") from e

    def chain_interrupt(self, vec_seg: int, vec_off: int, regs: dict,
                        cost: int) -> tuple:
        """ISR chain tail: a recovered interrupt handler jmp-far's to the
        PREVIOUS owner of its vector (the de-SMC'd EA target).  In the EXE-free
        image that owner is the BIOS IRET stub -- an explicit no-op platform
        effect, never a recovered-code dispatch (there is no recovered handler
        at a BIOS address)."""
        self._carrier.instruction_count = self._entry + cost
        return _chain_iret_stub(self.mem, vec_seg, vec_off, regs)

    def farcall(self, seg: int, off: int, regs: dict, argbytes: int,
                cost: int) -> dict:
        # The standalone backend does not yet own a platform-API device model
        # for import-thunk far-calls (the detached analogue of the DOS INT
        # services): fail loud with a witness until a real platform adapter is
        # bound, exactly as an unmodelled INT does above.  The VMless
        # verification binding routes these through the live API registry, so
        # the byte-exact differential runs today; the standalone runner needs a
        # bound API adapter to reach them.
        raise UnsupportedPlatformEffect(
            f"platform far-call {seg & 0xFFFF:04X}:{off & 0xFFFF:04X} "
            f"(argbytes {argbytes}) not implemented by the standalone CPUless "
            f"runtime: bind a platform-API adapter to service import thunks")

    def ivec(self, key, cost, regs):
        """Service a vectored interrupt whose target is NOT recovered game code.

        An ISR that saves its vector's previous contents and tail-chains to
        them -- the universal idiom -- holds whatever the environment left
        there, which in this runtime's power-on image is the BIOS IRET stub
        ``_init_bios_environment`` wrote at F000:FF53.  OVERKILL's IRQ0 handler
        ``1010:06E5`` does exactly this (``jmp far cs:[0738]``), so a cold
        CPUless run reaches it on the first chained tick.

        THE RULE (``runtime_core.install_bios_environment_hooks``): every
        ROM-BIOS entry a game can vector to must exist in the same form for
        EVERY runtime that can reach it, or the runtimes model different
        machines.  That rule was written for the interpreter and the VMless
        path.  The CPUless path is a third runtime that can reach it and did
        not have it -- the generated ``ivec_exec`` had no platform seam at all
        -- so a cold run died at the first chained tick with nothing in the
        game to blame.  This is the same drift the rule exists to stop, one
        runtime later.

        Delegates to the SAME :func:`_chain_iret_stub` the VMless verification
        binding uses, so the two model one machine by construction rather than
        by two implementations agreeing.  That helper VERIFIES the target: it
        reads the first byte from live memory and requires ``0xCF``, so an
        external handler that is not the stub still fails loud.  Anything that
        is not a well-formed ``SEG:OFF`` key is declined (``None``) and
        surfaces as the caller's frontier witness."""
        self._carrier.instruction_count = self._entry + cost
        try:
            seg, off = (int(part, 16) for part in key.split(":"))
        except ValueError:
            return None
        return _chain_iret_stub(self.mem, seg, off, regs)

    def boundary(self, head_cs, head_ip, resume_ip, regs, cost):
        """Boundary-head observer (standalone owner): advance the clock to
        the head and hand the pass to the scheduler callback -- which may
        PARK in-line (inputs + IRQs) and returns the possibly-updated
        bundle, flags word, and the extra virtual time the delivered ISRs
        executed."""
        self._carrier.instruction_count = self._entry + cost
        if self.boundary_cb is None:
            return regs, regs.get("_flags_in", 2), 0
        return self.boundary_cb(head_cs, head_ip, resume_ip, regs,
                                self._entry + cost)

    # -- recovered-root invocation ---------------------------------------

    def call(self, recovered_fn, **regs):
        """Invoke a recovered ROOT function against this runtime and advance
        the virtual clock by its reported cost.  Returns the ``(outputs,
        compat)`` pair the recovered function produced.  ``plat`` is passed
        only to functions whose contract takes it (they do platform effects);
        pure-compute functions take ``mem`` alone."""
        import inspect
        self._entry = self.clock
        params = inspect.signature(recovered_fn).parameters
        if "plat" in params:
            out, compat = recovered_fn(self.mem, self, **regs)
        else:
            out, compat = recovered_fn(self.mem, **regs)
        self.clock = self._entry + compat["cost"]
        return out, compat
