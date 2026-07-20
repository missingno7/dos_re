"""Focused tests for tools/liftlink.py + install.resolve_links — the batch linker.

The linking recipe in miniature, synthetic (game-free tests rule): edge
computation honours the link preconditions, and a two-function link runs
end-to-end THROUGH THE INSTALLER — modules written to disk with the
late-bound LINKS mechanism, loaded and resolved by graph activation,
executed on a real CPU8086, byte-exact against the interpreted original."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.lift.install import (_load_module, activate_generated_graph,
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
    0110 near-ret (linkable), 0120 retf (wrong exit shape), 0130 near-ret."""
    code = bytearray(b"\x90" * 0x40)
    code[0x00:0x0A] = bytes.fromhex("E80D00" "E81A00" "E82700" "C3")
    code[0x10] = 0xC3          # 0110: ret
    code[0x20] = 0xCB          # 0120: retf
    code[0x30] = 0xC3          # 0130: ret
    return bytes(code)


def test_edge_rule_links_every_structurally_eligible_target():
    # Proof status is separate evidence; return shape gates this transformation.
    scans = _scan_all(_edge_fixture_code(), [0x0100, 0x0110, 0x0120, 0x0130])
    edges, blocked = liftlink.compute_link_edges(scans)
    assert ("1000:0100", "1000:0110") in edges
    assert ("1000:0100", "1000:0130") in edges
    # retf callee called WITHOUT push cs: the sharper residue slug (a retf
    # callee is linkable only through the push-cs idiom, proven per site).
    assert ("1000:0100", "1000:0120", "retf-callee-no-push-cs-idiom") in blocked


def test_edge_rule_target_must_be_a_census_entry():
    scans = _scan_all(_edge_fixture_code(), [0x0100, 0x0110])   # 0120/0130 unlisted
    edges, blocked = liftlink.compute_link_edges(scans)
    assert edges == [("1000:0100", "1000:0110")]
    assert ("1000:0100", "1000:0120", "not-an-entry") in blocked
    assert ("1000:0100", "1000:0130", "not-an-entry") in blocked


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
    installed = activate_generated_graph(hyb, tmp_path)
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 8                   # 2 + 5 + 1
    assert hyb.s.sp == asm.s.sp
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data


def test_linked_caller_runs_direct_with_no_hooks_installed(tmp_path):
    """With an empty replacement-hook set, the
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

    asm = _make_cpu(code, _state())
    for _ in range(100):
        if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
            break
        asm.step()
    assert (asm.s.cs, asm.s.ip) == (CS, RET_IP)

    hyb = _make_cpu(code, _state())
    installed = activate_generated_graph(hyb, tmp_path)
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 8                   # 2 + 5 + 1
    assert hyb.s.sp == asm.s.sp                        # far frame fully popped
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data
    # Virtual time: identical clock (owns_time counted lift, both functions).
    assert hyb.instruction_count == asm.instruction_count


# --- the push-cs idiom: near-encoded far calls (push cs; call near -> retf) -----

def test_push_cs_near_call_to_retf_callee_links():
    """MSC compiles an intra-segment call to a FAR-returning function as
    ``push cs; call near``; the callee's retf pops the pushed CS + return IP.
    The edge links when every call site carries the provable push."""
    code = bytearray(b"\x90" * 0x20)
    code[0x00:0x05] = bytes.fromhex("0E" "E80C00" "C3")   # push cs; call 0110; ret
    code[0x10:0x14] = bytes.fromhex("050500" "CB")        # add ax,5; retf
    scans = _scan_all(bytes(code), [0x0100, 0x0110])
    assert liftlink.all_far_ret_exits(scans[(CS, 0x0110)])
    assert liftlink.push_cs_idiom_at_all_sites(scans[(CS, 0x0100)], 0x0110)
    edges, blocked = liftlink.compute_link_edges(scans)
    assert edges == [("1000:0100", "1000:0110")]
    assert not blocked


def test_push_cs_idiom_rejected_when_call_site_is_a_branch_target():
    """A call site that is also a branch target can be reached WITHOUT the
    push cs, so the far frame is not provable on every path -- blocked.
    Sequence adjacency within the same basic block is the evidence rule."""
    code = bytearray(b"\x90" * 0x20)
    # 0100: jz 0103 ; 0102: push cs ; 0103: call 0110 ; 0106: ret
    code[0x00:0x07] = bytes.fromhex("7401" "0E" "E80A00" "C3")
    code[0x10] = 0xCB                                     # 0110: retf
    scans = _scan_all(bytes(code), [0x0100, 0x0110])
    assert not liftlink.push_cs_idiom_at_all_sites(scans[(CS, 0x0100)], 0x0110)
    edges, blocked = liftlink.compute_link_edges(scans)
    assert edges == []
    assert ("1000:0100", "1000:0110", "retf-callee-no-push-cs-idiom") in blocked


def test_mixed_graph_links_only_qualifying_push_cs_edges():
    """One caller, three callees: a push-cs retf callee (links), a plain
    near-ret callee (links), and a retf callee without the push (honest
    residue)."""
    code = bytearray(b"\x90" * 0x50)
    code[0x00:0x0B] = bytes.fromhex(
        "0E"          # 0100: push cs
        "E81C00"      # 0101: call 0120   (idiom)
        "E82900"      # 0104: call 0130   (plain near-ret)
        "E83600"      # 0107: call 0140   (retf, NO push cs)
        "C3")         # 010A: ret
    code[0x20] = 0xCB                                     # 0120: retf
    code[0x30] = 0xC3                                     # 0130: ret
    code[0x40] = 0xCB                                     # 0140: retf
    scans = _scan_all(bytes(code), [0x0100, 0x0120, 0x0130, 0x0140])
    edges, blocked = liftlink.compute_link_edges(scans)
    assert ("1000:0100", "1000:0120") in edges
    assert ("1000:0100", "1000:0130") in edges
    assert ("1000:0100", "1000:0140", "retf-callee-no-push-cs-idiom") in blocked
    assert len(edges) == 2


def test_push_cs_linked_caller_and_retf_callee_run_exact(tmp_path):
    """End-to-end: the idiom edge links with the ORDINARY near-link emission.
    The caller's own emitted ``push cs`` supplies the segment word; the near
    helper pushes the return IP; the lifted callee's ``retf 2`` terminator
    pops IP, CS and the pascal argument -- exactly the interpreted frame,
    including SP, memory, flags and virtual time."""
    code = bytes.fromhex(
        "B80200"      # 0100: mov ax, 2
        "50"          # 0103: push ax            (pascal argument)
        "0E"          # 0104: push cs            (the idiom)
        "E80800"      # 0105: call 0x0110
        "050100"      # 0108: add ax, 1
        "C3"          # 010B: ret
        "90909090"    # pad to 0x0110
        "8BDC"        # 0110: mov bx, sp         (callee)
        "368B5F04"    # 0112: mov bx, ss:[bx+4]  (the arg: below IP and CS)
        "03C3"        # 0116: add ax, bx
        "CA0200")     # 0118: retf 2             (pops the arg too)
    scans = _scan_all(code, [0x0100, 0x0110])
    edges, _blocked = liftlink.compute_link_edges(scans)
    assert edges == [("1000:0100", "1000:0110")]

    callee_src = emit_function(scans[(CS, 0x0110)], CS, "lifted_1000_0110",
                               signature=code[0x10:0x14],
                               count_instructions=True)
    caller_src = liftlink.relink_source(scans[(CS, 0x0100)], CS, [0x0110],
                                        signature=code[:6])
    # The compensating push stays a counted native instruction; the call site
    # is the ordinary near link (no new ABI, no emulate_call left).
    assert "cpu.push(s.cs)" in caller_src
    assert "call_installed_hook_like_near_call" in caller_src
    assert 'LINKS["1000:0110"]' in caller_src
    assert "emulate_call" not in caller_src.split("def lifted_1000_0100")[1]
    (tmp_path / "lifted_1000_0110.py").write_text(callee_src, encoding="utf-8")
    (tmp_path / "lifted_1000_0100.py").write_text(caller_src, encoding="utf-8")

    asm = _make_cpu(code, _state())
    for _ in range(100):
        if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
            break
        asm.step()
    assert (asm.s.cs, asm.s.ip) == (CS, RET_IP)

    hyb = _make_cpu(code, _state())
    installed = activate_generated_graph(hyb, tmp_path)
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 5                   # 2 + 2 + 1
    assert hyb.s.sp == asm.s.sp                        # CS, IP and arg all popped
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data
    assert hyb.instruction_count == asm.instruction_count


# --- computed-return callee facts (MSC chkstk family) ---------------------------

def _chkstk_code() -> bytes:
    """0100: mov ax,2 ; call 0110 ; add ax,1 ; add sp,4 ; ret   (caller)
    0110: pop bx ; sub sp,4 ; jmp bx                            (near chkstk shape:
    returns to the caller's return address with SP deliberately BELOW the
    call point — the allocated frame)."""
    return bytes.fromhex("B80200" "E80A00" "050100" "83C404" "C3"
                         "909090"                     # pad to 0x0110
                         "5B" "83EC04" "FFE3")


def test_computed_return_near_fact_links_where_exit_shape_blocked():
    scans = _scan_all(_chkstk_code(), [0x0100, 0x0110])
    edges, blocked = liftlink.compute_link_edges(scans)
    assert edges == []                                  # jmp_ind exit: not linkable
    assert ("1000:0100", "1000:0110", "exit-shape") in blocked

    edges, blocked = liftlink.compute_link_edges(
        scans, computed_return_near=frozenset({"1000:0110"}))
    assert edges == [("1000:0100", "1000:0110")]
    assert not any(b[1] == "1000:0110" for b in blocked)


def test_computed_return_far_fact_gates_far_and_push_cs_edges():
    # Far callee ending in a computed far jump (jmp far [mem] after popping
    # the far return into memory — the __aFchkstk/__setargv shape), reached
    # through the push-cs idiom.  0100: push cs ; call 0110 ; ret
    # 0110: pop [0400] ; pop [0402] ; sub sp,4 ; jmp far [0400]
    code = bytearray(b"\x90" * 0x20)
    code[0x00:0x05] = bytes.fromhex("0E" "E80C00" "C3")
    code[0x10:0x1F] = bytes.fromhex("8F060004" "8F060204" "83EC04" "FF2E0004")
    scans = _scan_all(bytes(code), [0x0100, 0x0110])

    edges, blocked = liftlink.compute_link_edges(scans)
    assert edges == []
    assert ("1000:0100", "1000:0110", "exit-shape") in blocked

    # The far fact alone qualifies the callee; the push-cs site rule still
    # applies (same as a genuine retf callee).
    edges, _ = liftlink.compute_link_edges(
        scans, computed_return_far=frozenset({"1000:0110"}))
    assert edges == [("1000:0100", "1000:0110")]

    # And a DIRECT far call to the same callee links through the far edge set.
    far_edges, far_blocked = liftlink.compute_far_link_edges(
        scans, computed_return_far=frozenset({"1000:0110"}))
    assert far_edges == []                              # no far call site here
    edges2, blocked2 = liftlink.compute_link_edges(scans)
    assert ("1000:0100", "1000:0110", "exit-shape") in blocked2


def test_computed_return_near_linked_caller_runs_exact(tmp_path):
    """E2E: the linked chkstk-shaped edge runs byte-exact vs the interpreted
    original — the very case emulate_call can never terminate on (SP ends
    below the call point, so its unwind heuristic never fires)."""
    code = _chkstk_code()
    scans = _scan_all(code, [0x0100, 0x0110])

    (tmp_path / "lifted_1000_0110.py").write_text(
        emit_function(scans[(CS, 0x0110)], CS, "lifted_1000_0110",
                      signature=code[0x10:0x12], count_instructions=True),
        encoding="utf-8")
    (tmp_path / "lifted_1000_0100.py").write_text(
        liftlink.relink_source(scans[(CS, 0x0100)], CS, [0x0110],
                               signature=code[:6]), encoding="utf-8")

    asm = _make_cpu(code, _state())
    for _ in range(100):
        if (asm.s.cs, asm.s.ip) == (CS, RET_IP):
            break
        asm.step()
    assert (asm.s.cs, asm.s.ip) == (CS, RET_IP)

    hyb = _make_cpu(code, _state())
    installed = activate_generated_graph(hyb, tmp_path)
    assert set(installed) == {(CS, 0x0100), (CS, 0x0110)}
    hyb.step()

    assert (hyb.s.cs, hyb.s.ip) == (CS, RET_IP)
    assert hyb.s.ax == asm.s.ax == 3                   # 2 + 1
    assert hyb.s.sp == asm.s.sp
    assert (hyb.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
    assert hyb.mem.data == asm.mem.data
    assert hyb.instruction_count == asm.instruction_count
