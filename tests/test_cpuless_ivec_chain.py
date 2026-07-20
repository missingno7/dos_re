"""A chained interrupt vector pointing at ROM is ENVIRONMENT, not a frontier.

An ISR that saves its vector's previous contents and tail-chains to them is the universal DOS idiom,
and what it chains to is whatever the environment installed -- in this runtime's power-on image, the
BIOS IRET stub at F000:FF53.  The recovered corpus can never supply that target, so the generated
`ivec_exec` must offer it to the device model before raising its frontier witness.

`runtime_core.install_bios_environment_hooks` already states the rule -- every ROM-BIOS entry a game
can vector to must exist in the SAME form for every runtime that can reach it -- for the interpreter
and the VMless path.  The CPUless path is a third such runtime and had no seam at all: a cold run of
OVERKILL's corpus died at the first chained tick (`1010:06E5`, `jmp far cs:[0738]`) with nothing in
the game to blame.
"""
from __future__ import annotations

import pytest

from dos_re.lift.platform import CPUlessPlatformRuntime, UnsupportedPlatformEffect
from dos_re.memory import Memory

_BIOS_IRET = (0xF000, 0xFF53)


def _regs():
    r = {k: 0 for k in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es", "ss")}
    r["_flags_in"] = 0x246
    return r


def _runtime(tmp_path):
    mem = Memory()
    mem.data[0xFFF53] = 0xCF                      # the stub _init_bios_environment writes
    return CPUlessPlatformRuntime(mem, tmp_path), mem


def test_chaining_to_the_bios_iret_stub_is_serviced_as_one_iret(tmp_path):
    rt, _ = _runtime(tmp_path)
    regs = _regs()
    out, compat = rt.ivec("F000:FF53", 10, regs)
    assert out == regs, "an IRET leaves the register bundle alone"
    assert compat["cost"] == 0 and compat["fmask"] == 0
    assert compat["flags"] == 0x246


def test_a_target_that_is_not_the_stub_fails_loud(tmp_path):
    """The model is VERIFIED against live memory, not assumed from the address: an external handler
    that is real code must not be silently treated as a no-op."""
    rt, mem = _runtime(tmp_path)
    mem.data[0xFFF53] = 0x55                      # `push bp` -- a real handler, not the stub
    with pytest.raises(UnsupportedPlatformEffect) as e:
        rt.ivec("F000:FF53", 10, _regs())
    assert "F000:FF53" in str(e.value)


def test_a_malformed_key_is_declined_so_the_caller_still_raises(tmp_path):
    """Declining (None) must mean the caller raises its own witness naming the vector -- never that
    the interrupt silently did nothing."""
    rt, _ = _runtime(tmp_path)
    assert rt.ivec("not-a-vector", 10, _regs()) is None


def test_the_generated_dyncall_offers_unknown_vectors_to_the_platform():
    """The seam has to be IN the emitted support module, not just on the platform."""
    from dos_re.lift.emit_cpuless import DYNCALL_SUPPORT_SRC

    assert 'getattr(plat, "ivec", None)' in DYNCALL_SUPPORT_SRC
    body = DYNCALL_SUPPORT_SRC[DYNCALL_SUPPORT_SRC.index("def ivec_exec"):]
    assert "if key not in HANDLERS:" in body, "recovered handlers must still win"
    assert '_exec(HANDLERS, "ivec"' in body, "a declined vector must still reach the witness"
    # and the NEAR dispatch path must NOT have grown a platform escape hatch
    near = DYNCALL_SUPPORT_SRC[DYNCALL_SUPPORT_SRC.index("def dyn_exec"):
                               DYNCALL_SUPPORT_SRC.index("def ivec_exec")]
    assert "plat" not in near.replace("mem, plat, base", "")
