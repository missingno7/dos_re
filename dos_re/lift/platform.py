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

  * :class:`CPUlessPlatformRuntime` -- the STANDALONE backend used by
    ``play_cpuless.py``.  It owns a device model (a :class:`DOSMachine`, which
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
    """Standalone platform backend for ``play_cpuless.py``.

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
        #: the standalone SCHEDULER seam: play_cpuless installs a callback
        #: (head_cs, head_ip, resume_ip, regs, abs_cost) -> (regs, flags,
        #: extra_cost) that counts boundary-head passes and, on quota, PARKS
        #: in-line: applies demo inputs, delivers timer IRQs through the
        #: recovered HANDLERS, and returns the post-IRQ state.  Without a
        #: callback the observer is inert (free-running).
        self.boundary_cb = None
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
        if not hasattr(self, "_int_carrier"):
            self._int_carrier = _IntCarrier(self.mem)
        try:
            return _run_int(self.dos, self._int_carrier, num, regs,
                            self._entry + cost, regs.get("_flags", 0))
        except UnsupportedPlatformEffect:
            raise
        except HaltExecution:
            raise               # the program ended (int 21/4C): a real exit
        except Exception as e:  # noqa: BLE001 -- unset vector / unmodelled INT
            raise UnsupportedPlatformEffect(
                f"INT {num & 0xFF:02X} not implemented by the CPUless runtime "
                f"(unset vector or game-installed handler): {e}") from e

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
