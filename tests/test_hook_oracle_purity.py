"""The pure-ASM oracle: post-boot strip, and a loud guard against contamination.

A differential is evidence only if its reference side is the ORIGINAL program.
A registered replacement left on the "oracle" turns the run into a comparison
against a MODIFIED original -- and when the replacement is a deliberate
optimisation (skipping a loop pass, collapsing a wait) the oracle silently
diverges from the game while still looking authoritative.  The run does not
fail; it reports a WRONG frontier and sends the investigation after a defect
the candidate does not have.  That happened: a cold-start differential blamed
its candidate for a one-frame palette shift its own oracle was producing.

The mechanism that fails, and why it is replaced here:

    if install_replacements:        # <-- guarding the IMPORT does NOT work
        from . import hooks

Hooks register at DECORATION time, so the registry is populated the moment
anything anywhere in the process imports that module -- and something always
has, transitively.  ``install`` then wires it onto the "pure" CPU regardless.
The order-independent fix is to STRIP FROM THE REGISTRY AFTER BOOT, which is
what :meth:`HookRegistry.uninstall` does and what ``create_runtime`` now does
for ``install_replacements=False``.
"""
from __future__ import annotations

import pytest

from dos_re.hooks import HookRegistry, assert_pure_oracle


class _FakeCPU:
    """Only the two dicts the registry touches."""

    def __init__(self) -> None:
        self.replacement_hooks: dict = {}
        self.hook_names: dict = {}


def _registry_with(*addrs) -> HookRegistry:
    reg = HookRegistry()
    for cs, ip, name in addrs:
        reg.replace(cs, ip, name)(lambda cpu: None)
    return reg


_GAME = ((0x1010, 0x434A, "fade_loop_tick_gate"),
         (0x1010, 0x6168, "palette_upload"))
_BIOS_KEY = (0xF000, 0xE987)


def test_uninstall_strips_registered_hooks_but_not_framework_ones() -> None:
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    # a framework BIOS hook is installed OUTSIDE the registry
    cpu.replacement_hooks[_BIOS_KEY] = lambda cpu: None
    cpu.hook_names[_BIOS_KEY] = "bios_int9_keyboard"

    reg.uninstall(cpu)

    assert list(cpu.replacement_hooks) == [_BIOS_KEY]   # synthetic hardware stays
    assert list(cpu.hook_names) == [_BIOS_KEY]


def test_uninstall_honours_keep() -> None:
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    reg.uninstall(cpu, keep={(0x1010, 0x6168)})
    assert list(cpu.replacement_hooks) == [(0x1010, 0x6168)]


def test_stripping_is_import_order_independent() -> None:
    """The property the broken import-guard lacked.

    A registry populated BEFORE the CPU exists (the real case: some unrelated
    import pulled the hooks module in) must still yield a pure CPU.
    """
    reg = _registry_with(*_GAME)        # "already imported", registry populated
    cpu = _FakeCPU()
    reg.install(cpu)                    # create_runtime installs unconditionally
    reg.uninstall(cpu)                  # ...and the post-boot strip undoes it
    assert cpu.replacement_hooks == {}


def test_assert_pure_oracle_names_every_offender() -> None:
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    with pytest.raises(RuntimeError) as ei:
        assert_pure_oracle(cpu, registry_=reg)
    msg = str(ei.value)
    assert "1010:434A fade_loop_tick_gate" in msg
    assert "1010:6168 palette_upload" in msg
    # it must say WHY this invalidates the comparison, not just that it failed
    assert "MODIFIED original" in msg


def test_assert_pure_oracle_passes_on_a_stripped_cpu() -> None:
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    reg.uninstall(cpu)
    assert_pure_oracle(cpu, registry_=reg)          # must NOT raise


def test_assert_pure_oracle_allows_declared_environment_hooks() -> None:
    """A headless oracle may legitimately keep synthetic retrace/timer stand-ins
    -- but only ones it names explicitly."""
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    reg.uninstall(cpu, keep={(0x1010, 0x434A)})
    with pytest.raises(RuntimeError, match="fade_loop_tick_gate"):
        assert_pure_oracle(cpu, registry_=reg)      # undeclared -> still loud
    assert_pure_oracle(cpu, allow={(0x1010, 0x434A)}, registry_=reg)


def test_installed_on_reports_only_registered_replacements() -> None:
    reg = _registry_with(*_GAME)
    cpu = _FakeCPU()
    reg.install(cpu)
    cpu.replacement_hooks[_BIOS_KEY] = lambda cpu: None
    live = reg.installed_on(cpu)
    assert [k for k, _ in live] == [(0x1010, 0x434A), (0x1010, 0x6168)]
