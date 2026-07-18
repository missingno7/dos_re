"""The interpreter IP-DELTA probe -- the cross-check that keeps the static decoder honest.

``scan_function`` decodes a region statically and asks this probe, for each non-transfer instruction,
how far the INTERPRETER advances IP in one step. For a SEQ instruction that delta IS the encoded
length, so a disagreement means the static decode is wrong (an operand-length bug, or a transfer
misclassified as SEQ) and the function is refused ``decoder-mismatch``. That refusal is deliberately
fatal: silently lifting a mis-decoded instruction stream is the worst failure this pipeline has.

WHY THE PROBE MUST RESTORE MEMORY
---------------------------------
The probe EXECUTES each candidate instruction for real, at an arbitrary IP, with whatever register
state the previous probe step happened to leave behind. Those registers are meaningless -- the probe
only wants the instruction LENGTH -- but the execution is not: a `mov [bx],ax` with an unrelated `bx`
writes somewhere real. When that somewhere is the CODE SEGMENT, every later probe decodes the
corrupted bytes, and the mismatch it then reports is an artifact of the probe rather than a fact
about the program.

That is not theoretical. Probing OVERKILL's `1010:0248` left the region reading `3D FF 3D FF ...`;
`3D` is `cmp ax,imm16`, three bytes, so the probe reported ``interpreter-delta=3`` for instructions
whose true lengths were 5, 2 and 2 -- a constant 3 that looked like a decoder bug and was really the
probe overwriting its own subject. The function was refused, and the refusal cascaded: it blocked
`C679`, which blocked `4DBF`, which blocked `9B2E`, which is the callee the game's top-level frame
loop composes through -- so a stray write inside a diagnostic was, transitively, why the whole main
loop of the game could not be promoted.

So the probe restores the code segment after every step. Registers and non-code memory are left
alone: they cannot change how a later instruction DECODES, which is the only thing being measured.
"""
from __future__ import annotations


def make_ip_delta_probe(rt, cs: int, *, restore: bool = True):
    """An ``(ip) -> delta | None`` probe over a scratch clone of ``rt`` at segment ``cs``.

    Returns None when the interpreter could not execute there (the scan records it as *unchecked*,
    which is advisory) or when the step left IP unmoved.

    ``restore`` keeps the code segment pristine across probe steps and should stay on; it exists as a
    switch only so a test can demonstrate the corruption it prevents."""
    from dos_re.repro_artifacts import clone_runtime_state

    scratch = clone_runtime_state(rt)
    cpu = scratch.cpu
    # Hooks/tracing/IRQs would each make a step do something other than "execute this one
    # instruction", which is all the probe is asking about.
    cpu.replacement_hooks.clear()
    cpu.hook_names.clear()
    cpu.hook_verifier = None
    cpu.trace_enabled = False
    cpu.pending_irq = None

    seg = (cs & 0xFFFF) << 4
    end = seg + 0x10000
    mem = cpu.mem
    pristine = bytes(mem.data[seg:end]) if restore else None

    def probe(ip: int) -> "int | None":
        ip &= 0xFFFF
        cpu.s.cs = cs & 0xFFFF
        cpu.s.ip = ip
        try:
            cpu.step()
        except Exception:  # noqa: BLE001 -- the probe is advisory; the scan records the miss
            delta = None
        else:
            delta = ((cpu.s.ip - ip) & 0xFFFF) or None
        if pristine is not None:
            # Slice compare + assign: two C-level operations, so this is cheap enough to run on
            # every probe step. The common case (no write into the code segment) costs one memcmp.
            if mem.data[seg:end] != pristine:
                mem.data[seg:end] = pristine
        return delta

    return probe
