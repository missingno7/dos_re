"""Focused tests for tools/liftlink.py + install.resolve_links — the batch linker.

The de-VM pass in miniature, synthetic (game-free tests rule): edge
computation honours the link preconditions, and a two-function link runs
end-to-end THROUGH THE INSTALLER — modules written to disk with the
late-bound LINKS mechanism, loaded and resolved by install_passing_lifts,
executed on a real CPU8086, byte-exact against the interpreted original."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.lift.install import (_load_module, install_passing_lifts,
                                 resolve_links)
from dos_re.memory import Memory

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("liftlink", _TOOLS / "liftlink.py")
liftlink = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("liftlink", liftlink)
_spec.loader.exec_module(liftlink)

CS = 0x1000
ENTRY = 0x0100
RET_IP = 0xBEEF


def _scan_all(code: bytes, entry_ips):
    fetch = lambda off: code[off - ENTRY] if 0 <= (off - ENTRY) < len(code) else 0x90
    return {(CS, ip): scan_function(fetch, ip) for ip in entry_ips}


# --- edge computation: the link preconditions ----------------------------------

def _edge_fixture_code() -> bytes:
    """0100: call 0110 ; call 0120 ; call 0130 ; ret — three callee shapes:
    0110 near-ret (linkable), 0120 retf (wrong exit shape), 0130 near-ret
    but unproven."""
    code = bytearray(b"\x90" * 0x40)
    code[0x00:0x0A] = bytes.fromhex("E80D00" "E81A00" "E82700" "C3")
    code[0x10] = 0xC3          # 0110: ret
    code[0x20] = 0xCB          # 0120: retf
    code[0x30] = 0xC3          # 0130: ret
    return bytes(code)


def test_edge_rule_structural_default_links_without_proof():
    # DOS_RE 2.0 default: structural criterion only (liftable entry +
    # all-near-ret exits) — proof status is irrelevant, exit shape still gates.
    scans = _scan_all(_edge_fixture_code(), [0x0100, 0x0110, 0x0120, 0x0130])
    statuses = {"1000:0110": "ORACLE_PASSING", "1000:0120": "ORACLE_PASSING",
                "1000:0130": "NOT_REACHED"}
    edges, blocked = liftlink.compute_link_edges(scans, statuses)
    assert ("1000:0100", "1000:0110") in edges
    assert ("1000:0100", "1000:0130") in edges          # unproven: still linked
    assert ("1000:0100", "1000:0120", "exit-shape") in blocked


def test_edge_rule_proven_gate_requires_oracle_passing():
    # The 1.x conservative gate (--proven-edges / structural=False).
    scans = _scan_all(_edge_fixture_code(), [0x0100, 0x0110, 0x0120, 0x0130])
    statuses = {"1000:0110": "ORACLE_PASSING", "1000:0120": "ORACLE_PASSING",
                "1000:0130": "NOT_REACHED"}
    edges, blocked = liftlink.compute_link_edges(scans, statuses,
                                                 structural=False)
    assert edges == [("1000:0100", "1000:0110")]
    assert ("1000:0100", "1000:0120", "exit-shape") in blocked
    assert ("1000:0100", "1000:0130", "not-passing") in blocked


def test_edge_rule_target_must_be_a_census_entry():
    scans = _scan_all(_edge_fixture_code(), [0x0100, 0x0110])   # 0120/0130 unlisted
    statuses = {"1000:0110": "ORACLE_PASSING"}
    edges, blocked = liftlink.compute_link_edges(scans, statuses)
    assert edges == [("1000:0100", "1000:0110")]
    assert ("1000:0100", "1000:0120", "not-an-entry") in blocked
    assert ("1000:0100", "1000:0130", "not-an-entry") in blocked


def test_board_merge_oracle_passing_wins_across_boards(tmp_path):
    b1 = tmp_path / "b1.json"
    b1.write_text(json.dumps({"1000:0110": {"entry": "1000:0110",
                                            "status": "ORACLE_PASSING"}}))
    b2 = tmp_path / "b2.json"
    b2.write_text(json.dumps({"1000:0110": {"entry": "1000:0110",
                                            "status": "NOT_REACHED"},
                              "1000:0130": {"entry": "1000:0130",
                                            "status": "DIVERGED"}}))
    statuses = liftlink.load_statuses([b1, b2])
    assert statuses["1000:0110"] == "ORACLE_PASSING"   # any pass qualifies
    assert statuses["1000:0130"] == "DIVERGED"


# --- the end-to-end link, THROUGH THE INSTALLER --------------------------------

def _e2e_code() -> bytes:
    """0100: mov ax,2 ; call 0110 ; add ax,1 ; ret   (caller)
    0110: add ax,5 ; ret                             (callee)"""
    return bytes.fromhex("B80200" "E80A00" "050100" "C3"
                         "909090909090"              # pad to 0x0110
                         "050500" "C3")


def _make_cpu(code: bytes, st: CPUState) -> CPU8086:
    mem = Memory()
    mem.load(CS, ENTRY, code)
    cpu = CPU8086(mem, st)
    cpu.trace_enabled = False
    cpu.push(RET_IP)
    return cpu


def _state() -> CPUState:
    return CPUState(ax=0, bx=0, cx=1, dx=0, sp=0x2000, cs=CS, ip=ENTRY,
                    ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)


def test_linked_two_function_module_set_installs_and_runs_exact(tmp_path):
    code = _e2e_code()
    scans = _scan_all(code, [0x0100, 0x0110])

    # Emit the callee as liftverify would; the caller through the LINKER.
    callee_src = emit_function(scans[(CS, 0x0110)], CS, "lifted_1000_0110",
                               signature=code[0x10:0x14], coverage=True)
    caller_src = liftlink.relink_source(scans[(CS, 0x0100)], CS, [0x0110],
                                        signature=code[:6])
    (tmp_path / "lifted_1000_0110.py").write_text(callee_src, encoding="utf-8")
    (tmp_path / "lifted_1000_0100.py").write_text(caller_src, encoding="utf-8")

    # The linked edge left no emulate_call in the caller's BODY.
    assert "emulate_call" not in caller_src.split("def lifted_1000_0100")[1]
    assert liftlink.count_emulate_calls(caller_src) == 0
    assert 'LINKS["1000:0110"]' in caller_src

    # The linked module still loads STANDALONE (flat loader, callee unbound).
    alone = _load_module(tmp_path / "lifted_1000_0100.py")
    assert alone.LINKS == {"1000:0110": None}

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "1000:0100": {"entry": "1000:0100", "module": "lifted_1000_0100.py",
                      "status": "ORACLE_PASSING"},
        "1000:0110": {"entry": "1000:0110", "module": "lifted_1000_0110.py",
                      "status": "ORACLE_PASSING"},
    }))

    # Oracle: the interpreted original.
    asm = _make_cpu(code, _state())
    for _ in range(100):
        if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
            break
        asm.step()
    assert (asm.s.cs, asm.s.ip) == (CS, RET_IP)

    # Hybrid: install through the real installer (two-pass load + LINKS
    # resolution), then one step() dispatches the caller hook.
    hyb = _make_cpu(code, _state())
    installed = install_passing_lifts(hyb, CS, tmp_path, [manifest])
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 8                   # 2 + 5 + 1
    assert hyb.s.sp == asm.s.sp
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data


def test_linked_caller_runs_direct_with_no_hooks_installed(tmp_path):
    """The VM-less degradation: with an EMPTY replacement_hooks set, the
    caller's linked call still reaches the callee through its resolved LINKS
    binding (no interpreter, no hook table)."""
    code = _e2e_code()
    scans = _scan_all(code, [0x0100, 0x0110])
    (tmp_path / "lifted_1000_0110.py").write_text(
        emit_function(scans[(CS, 0x0110)], CS, "lifted_1000_0110",
                      signature=code[0x10:0x14]), encoding="utf-8")
    (tmp_path / "lifted_1000_0100.py").write_text(
        liftlink.relink_source(scans[(CS, 0x0100)], CS, [0x0110],
                               signature=code[:6]), encoding="utf-8")
    loaded = {"lifted_1000_0100": _load_module(tmp_path / "lifted_1000_0100.py")}
    assert resolve_links(loaded, tmp_path) == 1
    assert "lifted_1000_0110" in loaded                # pulled in from disk

    cpu = _make_cpu(code, _state())
    assert not cpu.replacement_hooks
    loaded["lifted_1000_0100"].lifted_1000_0100(cpu)
    assert (cpu.s.cs, cpu.s.ip) == (CS, RET_IP)
    assert cpu.s.ax == 8


# --- resolve_links: the installer's second pass ---------------------------------

def test_resolve_links_binds_transitively(tmp_path):
    (tmp_path / "lifted_1000_0100.py").write_text(
        'LINKS = {"1000:0110": None}\n'
        "def lifted_1000_0100(cpu):\n    return LINKS[\"1000:0110\"](cpu)\n")
    (tmp_path / "lifted_1000_0110.py").write_text(
        'LINKS = {"1000:0120": None}\n'
        "def lifted_1000_0110(cpu):\n    return LINKS[\"1000:0120\"](cpu)\n")
    (tmp_path / "lifted_1000_0120.py").write_text(
        "def lifted_1000_0120(cpu):\n    cpu.value = 7\n")
    loaded = {"lifted_1000_0100": _load_module(tmp_path / "lifted_1000_0100.py")}
    assert resolve_links(loaded, tmp_path) == 2        # two slots, chain followed

    class Probe:
        value = 0
    probe = Probe()
    loaded["lifted_1000_0100"].lifted_1000_0100(probe)
    assert probe.value == 7                            # 0100 -> 0110 -> 0120


def test_resolve_links_missing_callee_fails_loud(tmp_path):
    (tmp_path / "lifted_1000_0100.py").write_text(
        'LINKS = {"1000:0999": None}\n'
        "def lifted_1000_0100(cpu):\n    pass\n")
    loaded = {"lifted_1000_0100": _load_module(tmp_path / "lifted_1000_0100.py")}
    with pytest.raises(FileNotFoundError, match="1000:0999"):
        resolve_links(loaded, tmp_path)


def test_far_linked_caller_and_retf_callee_run_exact(tmp_path):
    """The FAR mirror of the linked e2e: caller far-calls a retf callee; the
    linked module set installs through the real installer and runs byte-exact
    (state + virtual time) against the interpreted original."""
    # 0100: mov ax,2 ; call far 1000:0110 ; add ax,1 ; ret     (caller)
    # 0110: add ax,5 ; retf                                    (callee)
    code = bytes.fromhex("B80200" "9A10010010" "050100" "C3"
                         "9090909090"                # pad to 0x0110
                         "050500" "CB")
    scans = _scan_all(code, [0x0100, 0x0110])
    assert liftlink.all_far_ret_exits(scans[(CS, 0x0110)])
    assert not liftlink.all_near_ret_exits(scans[(CS, 0x0110)])

    # Far edge set: structural rule finds exactly the one edge.
    edges, blocked = liftlink.compute_far_link_edges(scans)
    assert edges == [("1000:0100", "1000:0110")]

    callee_src = emit_function(scans[(CS, 0x0110)], CS, "lifted_1000_0110",
                               signature=code[0x10:0x14],
                               count_instructions=True)
    caller_src = liftlink.relink_source(scans[(CS, 0x0100)], CS, [],
                                        far_targets=[(CS, 0x0110)],
                                        signature=code[:6])
    assert "call_installed_hook_like_far_call" in caller_src
    assert "emulate_far_call" not in caller_src.split("def lifted_1000_0100")[1]
    assert 'LINKS["1000:0110"]' in caller_src
    (tmp_path / "lifted_1000_0110.py").write_text(callee_src, encoding="utf-8")
    (tmp_path / "lifted_1000_0100.py").write_text(caller_src, encoding="utf-8")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "1000:0100": {"entry": "1000:0100", "module": "lifted_1000_0100.py",
                      "status": "ORACLE_PASSING"},
        "1000:0110": {"entry": "1000:0110", "module": "lifted_1000_0110.py",
                      "status": "ORACLE_PASSING"},
    }))

    asm = _make_cpu(code, _state())
    for _ in range(100):
        if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
            break
        asm.step()
    assert (asm.s.cs, asm.s.ip) == (CS, RET_IP)

    hyb = _make_cpu(code, _state())
    installed = install_passing_lifts(hyb, CS, tmp_path, [manifest])
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 8                   # 2 + 5 + 1
    assert hyb.s.sp == asm.s.sp                        # far frame fully popped
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data
    # Virtual time: identical clock (owns_time counted lift, both functions).
    assert hyb.instruction_count == asm.instruction_count
