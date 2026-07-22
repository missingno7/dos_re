"""dos_re.lift.dispatch.static_switch_targets: the bounded, cs-relative switch
jump table is STATIC evidence -- the one indirect-jump idiom readable from the
image alone.

Synthetic hand-assembled functions only (game-free tests rule).  The matcher
must be refuse-on-doubt: every deviation from the proven idiom returns None
(the site stays a dynamic frontier), never a guessed arm set.
"""
from __future__ import annotations

import json

from dos_re.atlas import ExecutionAtlas
from dos_re.identity import (
    ExecutionPointIdentity, FunctionIdentity, ImageIdentity, ProgramIdentity,
    real_mode_address)
from dos_re.lift.cfg import scan_function
from dos_re.lift.dispatch import static_switch_targets
from dos_re.lift.irgen_core import function_record

PROGRAM = ProgramIdentity("fixture:1")
IMAGE = ImageIdentity(PROGRAM, "fixture-exe", "sha256", "b" * 64)


def _fetch(code, base: int):
    return lambda off: code[(off - base) & 0xFFFF]


def _switch_code(base=0x0100):
    """The MSC switch idiom, jbe polarity:

        0100: cmp ax, 3          ; bound -> 4 entries
        0103: jbe 0108           ; enters the dispatch tail
        0105: jmp 014A           ; default
        0108: shl ax, 1
        010A: xchg bx, ax
        010B: jmp word cs:[bx+0120]
        0120: table [0130, 013A, 0140, 0130]
        014A: ret (default), plus rets at each arm
    """
    code = bytearray(b"\x90" * 0x100)
    code[0x00:0x03] = bytes.fromhex("3D0300")
    code[0x03:0x05] = bytes.fromhex("7603")
    code[0x05:0x08] = bytes.fromhex("E94200")
    code[0x08:0x0A] = bytes.fromhex("D1E0")
    code[0x0A:0x0B] = bytes.fromhex("93")
    code[0x0B:0x10] = bytes.fromhex("2EFFA72001")
    for i, arm in enumerate((0x0130, 0x013A, 0x0140, 0x0130)):
        code[0x20 + 2 * i:0x22 + 2 * i] = arm.to_bytes(2, "little")
    for off in (0x30, 0x3A, 0x40, 0x4A):
        code[off] = 0xC3
    return code


def test_bounded_cs_table_is_read_jbe_polarity():
    fetch = _fetch(_switch_code(), 0x0100)
    scan = scan_function(fetch, 0x0100)
    table, count, arms = static_switch_targets(scan, 0x010B, fetch)
    assert table == 0x0120
    assert count == 4
    assert arms == (0x0130, 0x013A, 0x0140, 0x0130)   # index order, dup kept


def test_ja_polarity_and_cmp_imm8_form():
    # 0200: cmp ax, 2 (83 F8 02); ja 020D (default); shl; xchg;
    # 0208: jmp cs:[bx+0220]; 020D: ret; table [0230, 0234, 0238]
    code = bytearray(b"\x90" * 0x100)
    code[0x00:0x03] = bytes.fromhex("83F802")
    code[0x03:0x05] = bytes.fromhex("7708")
    code[0x05:0x07] = bytes.fromhex("D1E0")
    code[0x07:0x08] = bytes.fromhex("93")
    code[0x08:0x0D] = bytes.fromhex("2EFFA72002")
    code[0x0D] = 0xC3
    for i, arm in enumerate((0x0230, 0x0234, 0x0238)):
        code[0x20 + 2 * i:0x22 + 2 * i] = arm.to_bytes(2, "little")
    for off in (0x30, 0x34, 0x38):
        code[off] = 0xC3
    fetch = _fetch(code, 0x0200)
    scan = scan_function(fetch, 0x0200)
    assert static_switch_targets(scan, 0x0208, fetch) == (
        0x0220, 3, (0x0230, 0x0234, 0x0238))


def test_refusals_stay_a_dynamic_frontier():
    base = 0x0100
    # (a) no CS override -- a DS-relative table is not readable from code.
    code = _switch_code()
    code[0x0B:0x10] = bytes.fromhex("FFA7200190")   # jmp [bx+0120] + pad
    fetch = _fetch(code, base)
    assert static_switch_targets(
        scan_function(fetch, base), 0x010B, fetch) is None

    # (b) a foreign instruction between the guard and the jump.
    code = _switch_code()
    code[0x0A] = 0x40                               # inc ax instead of xchg
    fetch = _fetch(code, base)
    assert static_switch_targets(
        scan_function(fetch, base), 0x010B, fetch) is None

    # (c) wrong guard polarity: ja INTO the tail is not the idiom.
    code = _switch_code()
    code[0x03] = 0x77                               # ja 0108 (enters tail)
    fetch = _fetch(code, base)
    assert static_switch_targets(
        scan_function(fetch, base), 0x010B, fetch) is None

    # (d) a zero table word is no plausible arm -- refuse the whole site.
    code = _switch_code()
    code[0x22:0x24] = b"\x00\x00"
    fetch = _fetch(code, base)
    assert static_switch_targets(
        scan_function(fetch, base), 0x010B, fetch) is None

    # (e) a negative imm8 bound is nonsense for a table index.
    code = _switch_code()
    code[0x00:0x03] = bytes.fromhex("83F890")
    fetch = _fetch(code, base)
    assert static_switch_targets(
        scan_function(fetch, base), 0x010B, fetch) is None


def test_prescaled_index_refuses_the_bound_is_a_byte_offset():
    """Found on the first Win16 corpus (SIMANTW _GBoxFill's ROP dispatch):
    `shr ax,3; cmp ax,0C; ja; xchg; jmp cs:[bx+T]` bounds an ALREADY-SCALED
    byte offset -- 7 entries, not 13.  Without the post-guard `shl` there is
    no proof the bound counts entries, so the matcher must refuse; reading
    bound+1 words walks past the table into code bytes (phantom arms)."""
    code = bytearray(b"\x90" * 0x100)
    code[0x00:0x03] = bytes.fromhex("257000")       # and ax, 0x0070
    code[0x03:0x06] = bytes.fromhex("C1E803")       # shr ax, 3  (pre-scale)
    code[0x06:0x09] = bytes.fromhex("3D0C00")       # cmp ax, 0x000C
    code[0x09:0x0B] = bytes.fromhex("7705")         # ja 0110 (default)
    code[0x0B:0x0C] = bytes.fromhex("93")           # xchg bx, ax
    code[0x0C:0x11] = bytes.fromhex("2EFFA72001")   # jmp cs:[bx+0120]
    code[0x11] = 0xC3
    for i in range(7):
        code[0x20 + 2 * i:0x22 + 2 * i] = (0x0130 + 2 * i).to_bytes(2, "little")
    code[0x30:0x40] = b"\xc3" * 16
    fetch = _fetch(code, 0x0100)
    scan = scan_function(fetch, 0x0100)
    assert static_switch_targets(scan, 0x010C, fetch) is None


def test_irgen_annotates_the_site_with_static_targets():
    fetch = _fetch(_switch_code(), 0x0100)
    scan = scan_function(fetch, 0x0100)
    record = function_record(scan, 0x1010, 0x0100, fetch, set())
    sites = [inst for block in record["blocks"]
             for inst in block["instructions"] if inst["kind"] == "jmp_ind"]
    assert len(sites) == 1
    assert sites[0]["static_table"] == "0120"
    assert sites[0]["static_targets"] == ["0130", "013A", "0140"]


def _function(offset: int) -> str:
    return str(FunctionIdentity(
        IMAGE, "real-mode", real_mode_address(0x1010, offset)))


def _point(offset: int) -> str:
    return str(ExecutionPointIdentity(
        IMAGE, "real-mode", real_mode_address(0x1010, offset)))


def test_atlas_import_resolves_static_switch_edges(tmp_path):
    """static_targets in the IR become RESOLVED site->arm edges: the site is
    no longer an unresolved frontier, and coverage reaches every arm without
    any replay observation."""
    document = {
        "ir_version": 0,
        "provenance": {"snapshot": "fixture", "toolchain": "fixture"},
        "facts_applied": [],
        "functions": {
            "1010:0100": {
                "entry": "1010:0100", "liftable": True, "refusals": [],
                "exits": ["jmp_ind"], "signature": "2effa72001",
                "calls_near": [], "calls_far": [], "ints": [],
                "blocks": [{
                    "leader": "0100",
                    "instructions": [
                        {"ip": "0100", "bytes": "2effa72001",
                         "kind": "jmp_ind", "mnemonic": "jmp rm16",
                         "mem_operand": True,
                         "static_table": "0120",
                         "static_targets": ["0300", "0400"]},
                    ],
                }],
            },
            "1010:0200": {          # a plain unresolved site stays a frontier
                "entry": "1010:0200", "liftable": True, "refusals": [],
                "exits": ["jmp_ind"], "signature": "ffe3",
                "calls_near": [], "calls_far": [], "ints": [],
                "blocks": [{
                    "leader": "0200",
                    "instructions": [
                        {"ip": "0200", "bytes": "ffe3", "kind": "jmp_ind",
                         "mnemonic": "jmp bx"},
                    ],
                }],
            },
            "1010:0300": {          # arm carved as a census entry
                "entry": "1010:0300", "liftable": True, "refusals": [],
                "exits": ["ret"], "signature": "c3",
                "calls_near": [], "calls_far": [], "ints": [],
                "blocks": [{"leader": "0300", "instructions": [
                    {"ip": "0300", "bytes": "c3", "kind": "ret",
                     "mnemonic": "ret"}]}],
            },
        },
        "unsupported": [],
    }
    ir = tmp_path / "ir.json"
    ir.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    atlas = ExecutionAtlas.create(tmp_path / "atlas", program=PROGRAM)
    atlas.import_recovery_ir(ir, image=IMAGE, roots=["1010:0100"])

    site, other_site = _point(0x0100), _point(0x0200)
    edges = atlas.edges()
    # The annotated site is RESOLVED (both the containment-side edge and the
    # site->arm transfers); the plain site stays the honest frontier.
    by_pair = {(e.source, e.target, e.kind): e for e in edges}
    assert by_pair[(_function(0x0100), site, "jmp_ind")].status == "resolved"
    assert by_pair[(
        _function(0x0100), site, "jmp_ind")].metadata["table"] == "0120"
    assert by_pair[(site, _function(0x0300), "jmp_ind")].status == "resolved"
    # An arm with no census entry is a KNOWN target without a carved function.
    assert by_pair[(site, _point(0x0400), "jmp_ind")].status == "frontier"
    assert by_pair[(
        _function(0x0200), other_site, "jmp_ind")].status == "unresolved"
    # unresolved(): the plain site, PLUS the frontier edge to the arm with no
    # census entry (a known address with no carved function is residual work,
    # exactly like a call-far frontier).  The resolved site itself is gone.
    assert {(e.source, e.status) for e in atlas.unresolved()} == {
        (_function(0x0200), "unresolved"), (site, "frontier")}

    atlas.set_product_roots("development", [_function(0x0100)])
    coverage = atlas.coverage_for("development")
    assert _function(0x0300) in coverage.reachable   # via the static arms
    # The entry-less arm is frontier: known address, no carved function, so it
    # does not extend coverage (same semantics as a call-far frontier).  A
    # census that closes over static_targets turns it into a function entry.
    assert _point(0x0400) not in coverage.reachable
