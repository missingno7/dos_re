"""Dead-register-output pruning: sound, and a no-op under the conservative seed.

The pruner keeps a register output only if it is live at some clean return exit
(AbiReport.exit_live). Under the current whole-register-file boundary model,
abi_scan seeds EVERY may-written register live at exit, so exit_live == outputs
minus sp and the prune removes nothing -- which is the point: it proves the
emitted output set is already minimal. These tests pin both the mechanism
(exit_live is computed and sound) and the current result (0 removed), so a
future inter-procedural narrowing has to update them deliberately.
"""
from __future__ import annotations

from dos_re.lift.cfg import scan_function
from dos_re.lift.cpuless import abi_scan
from dos_re.lift.emit_cpuless import _output_set, output_prune_removed


def _scan(code: bytes):
    return scan_function(lambda o: code[o] if o < len(code) else 0x90, 0)


def test_exit_live_equals_outputs_minus_sp_under_conservative_seed() -> None:
    # mov ax,5 ; mov bx,ax ; ret -- writes ax,bx; the seed makes both live at exit.
    abi = abi_scan(_scan(bytes.fromhex("b80500" "89c3" "c3")))
    assert abi.exit_live is not None
    assert abi.exit_live == (abi.outputs - frozenset({"sp"}))


def test_prune_removes_nothing_and_is_a_noop() -> None:
    abi = abi_scan(_scan(bytes.fromhex("b80500" "89c3" "c3")))
    full = sorted((abi.outputs & (frozenset({"ax", "cx", "dx", "bx", "sp",
                                             "bp", "si", "di", "ds", "es"})))
                  - frozenset({"sp"}))
    assert _output_set(abi, sp_output=False) == full     # identical to pre-prune
    assert output_prune_removed(abi, sp_output=False) == []


def test_tail_exit_function_retains_all_outputs() -> None:
    # jmp far -- a non-return terminal governs live-out, so exit_live is None
    # and the pruner must retain everything (never prune against a RET-only view).
    abi = abi_scan(_scan(bytes.fromhex("b80500" "ea00000010")))   # mov ax,5; jmp far
    assert abi.exit_live is None
    assert output_prune_removed(abi, sp_output=False) == []


def test_sp_survives_the_liveness_filter_when_sp_output() -> None:
    # exit_live never carries sp; sp_output must still keep it in the output set.
    abi = abi_scan(_scan(bytes.fromhex("b80500" "c3")))
    assert "sp" in _output_set(abi, sp_output=True)
    assert "sp" not in _output_set(abi, sp_output=False)
