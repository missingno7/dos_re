"""Smoke tests for ``dos_re.runtime_code`` (polyvariant runtime-patched-code
support), promoted from Overkill's own ``overkill/runtime_code.py`` -- the
mechanism was already game-agnostic; only the per-game slot table stayed
behind as caller-supplied data."""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086
from dos_re.memory import Memory
from dos_re.runtime_code import (
    RuntimeCodeSlot,
    RuntimeCodeStaticization,
    RuntimeCodeStaticizationError,
    RuntimeCodeVariant,
    RuntimeCodeWriteTracer,
    UnknownRuntimeCodeVariant,
    assert_runtime_code_staticization_ready,
    default_runtime_code_regions,
    identify_runtime_code_variant,
    require_runtime_code_variant,
    runtime_code_staticization_report,
)

ADDR = (0x1010, 0x0100)


def _cpu_with_bytes(data: bytes) -> CPU8086:
    cpu = CPU8086(Memory())
    seg, off = ADDR
    for i, b in enumerate(data):
        cpu.mem.wb(seg, off + i, b)
    return cpu


def _slots(*variants: RuntimeCodeVariant, staticization=None, writer_status="observed") -> dict:
    slot = RuntimeCodeSlot(
        addr=ADDR, name="slot", subsystem="test", owner=None, role="test slot",
        variants=variants, staticization=staticization, writer_status=writer_status,
    )
    return {ADDR: slot}


def test_identify_matches_known_variant_by_signature():
    accepted = RuntimeCodeVariant(addr=ADDR, name="accepted", signature=b"\x90\x90", subsystem="x", status="staticized-verified")
    other = RuntimeCodeVariant(addr=ADDR, name="cold", signature=b"\xCC\xCC", subsystem="x", status="observed-only")
    slots = _slots(accepted, other)
    cpu = _cpu_with_bytes(b"\x90\x90")

    variant = identify_runtime_code_variant(cpu, ADDR, slots)
    assert variant.name == "accepted"
    assert accepted.is_accepted_runtime_body
    assert not other.is_accepted_runtime_body


def test_identify_raises_on_unregistered_address():
    with pytest.raises(UnknownRuntimeCodeVariant):
        identify_runtime_code_variant(_cpu_with_bytes(b"\x90"), ADDR, {})


def test_identify_raises_on_unknown_bytes():
    accepted = RuntimeCodeVariant(addr=ADDR, name="accepted", signature=b"\x90\x90", subsystem="x", status="staticized-verified")
    cpu = _cpu_with_bytes(b"\x11\x22")
    with pytest.raises(UnknownRuntimeCodeVariant):
        identify_runtime_code_variant(cpu, ADDR, _slots(accepted))


def test_require_rejects_known_but_wrong_variant():
    accepted = RuntimeCodeVariant(addr=ADDR, name="accepted", signature=b"\x90\x90", subsystem="x", status="staticized-verified")
    slots = _slots(accepted)
    cpu = _cpu_with_bytes(b"\x90\x90")
    assert require_runtime_code_variant(cpu, ADDR, "accepted", slots).name == "accepted"
    with pytest.raises(UnknownRuntimeCodeVariant):
        require_runtime_code_variant(cpu, ADDR, "some_other_hook", slots)


def test_staticization_report_and_gate():
    accepted = RuntimeCodeVariant(addr=ADDR, name="accepted", signature=b"\x90\x90", subsystem="x", status="staticized-verified")
    slots = _slots(accepted)

    report = runtime_code_staticization_report(slots)
    assert report[0]["missing"] == ("static source target",)
    with pytest.raises(RuntimeCodeStaticizationError):
        assert_runtime_code_staticization_ready(slots)

    staticized = _slots(
        accepted,
        staticization=RuntimeCodeStaticization(
            source_module="game.recovered", source_function="run_slot", dispatch="variant_guarded_static_hook",
        ),
    )
    assert_runtime_code_staticization_ready(staticized)  # must not raise


def test_write_tracer_fires_only_inside_registered_regions():
    accepted = RuntimeCodeVariant(addr=ADDR, name="accepted", signature=b"\x90\x90", subsystem="x", status="staticized-verified")
    slots = _slots(accepted)
    cpu = _cpu_with_bytes(b"\x90\x90")
    regions = default_runtime_code_regions(slots)

    tracer = RuntimeCodeWriteTracer(cpu, regions).install()
    try:
        cpu.mem.wb(*ADDR, 0xCC)  # inside the region -> traced
        cpu.mem.wb(ADDR[0], ADDR[1] + 0x1000, 0xCC)  # far outside -> not traced
    finally:
        tracer.uninstall()

    assert len(tracer.events) == 1
    assert tracer.events[0].writer == (cpu.s.cs, cpu.s.ip)
