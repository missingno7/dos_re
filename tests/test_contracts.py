"""ABI-contract inference (dos_re.lift.contracts).

Locks contract proposals on a small synthetic recovery IR: interprocedural
composition, caller-observed return narrowing, stack-argument evidence,
pointer-pair evidence, and the refusal taxonomy.
"""
from __future__ import annotations

from dos_re.lift.contracts import (build_scans, compose_effects,
                                   infer_contracts, observed_returns,
                                   pointer_pairs_of, stack_args_of)
from dos_re.lift.decode import decode_one


def _record(entry_key: str, hexbytes: str) -> dict:
    """A minimal recovery-IR record: pinned instruction bytes re-elaborated
    by the real scanner (dos_re.lift.ir.scan_from_ir_record)."""
    raw = bytes.fromhex(hexbytes.replace(" ", ""))
    base = int(entry_key.split(":")[1], 16)
    insts = []
    ip = 0
    while ip < len(raw):
        inst = decode_one(lambda off, _r=raw, _b=base: _r[off - _b],
                          base + ip)
        insts.append({"ip": f"{base + ip:04X}",
                      "bytes": inst.raw.hex().upper()})
        ip += inst.length
    return {"entry": entry_key, "liftable": True, "refusals": [],
            "blocks": [{"instructions": insts}], "signature": raw[:4].hex()}


def _ir(funcs: dict[str, str]) -> dict:
    return {"ir_version": 0,
            "functions": {k: _record(k, hx) for k, hx in funcs.items()}}


#: callee 1010:0100 -- writes ax and bx, returns near.
CALLEE = "B8 05 00"     "BB 07 00"     "C3"        # mov ax,5; mov bx,7; ret
#: caller 1010:0000 -- calls 0100, observes ONLY ax (bx is overwritten).
CALLER = "E8 FD 00"     "89 C1"        "BB 01 00"  "C3"
#         call 0100     mov cx,ax      mov bx,1     ret


def test_compose_effects_interprocedural():
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    scans, skipped = build_scans(ir)
    assert not skipped
    effects, reports, notes = compose_effects(ir, scans)
    ins, outs = effects["1010:0100"]
    assert "ax" not in ins and {"ax", "bx"} <= outs
    cins, couts = effects["1010:0000"]
    # the callee supplies ax: reading it after the call is NOT a caller input
    assert "ax" not in cins
    assert {"ax", "bx", "cx"} <= couts
    assert not notes.get("1010:0000") and not notes.get("1010:0100")


def test_observed_returns_narrow_to_caller_use():
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    scans, _ = build_scans(ir)
    effects, _, _ = compose_effects(ir, scans)
    observed, status = observed_returns(ir, scans, effects)
    # the caller reads ax and overwrites bx -> only ax is observed
    assert status["1010:0100"] == "narrowed"
    assert observed["1010:0100"] == frozenset({"ax"})
    # the caller itself has no static caller -> conservative, full outputs
    assert status["1010:0000"] == "no-static-caller"
    assert {"ax", "bx", "cx"} <= observed["1010:0000"]


def test_observed_returns_external_stays_conservative():
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    scans, _ = build_scans(ir)
    effects, _, _ = compose_effects(ir, scans)
    observed, status = observed_returns(
        ir, scans, effects, external=frozenset({"1010:0100"}))
    # dispatch/vector reachability forbids narrowing
    assert status["1010:0100"] == "external-conservative"
    assert {"ax", "bx"} <= observed["1010:0100"]


def test_stack_args_framed_ret_n():
    # push bp; mov bp,sp; mov ax,[bp+4]; mov dx,[bp+6]; pop bp; ret 4
    ir = _ir({"1010:0000":
              "55 89 E5 8B 46 04 8B 56 06 5D C2 04 00"})
    scans, _ = build_scans(ir)
    rep = stack_args_of(scans["1010:0000"])
    assert rep.framed and rep.ret_kind == "near"
    assert rep.ret_pop == 4 and rep.arg_bytes == 4
    assert rep.arg_slots == ((0, 2), (2, 2))
    assert not rep.refusals


def test_stack_args_extent_conflict_refuses():
    # framed, reads [bp+4] and [bp+6], but ret 2 only pops ONE arg word
    ir = _ir({"1010:0000":
              "55 89 E5 8B 46 04 8B 56 06 5D C2 02 00"})
    scans, _ = build_scans(ir)
    rep = stack_args_of(scans["1010:0000"])
    assert "frame-arg-extent-exceeds-ret-pop" in rep.refusals


def test_unframed_bp_is_not_stack_machinery():
    # mov ax,[bp+2]; ret -- bp is a live-in POINTER, not a frame
    ir = _ir({"1010:0000": "8B 46 02 C3"})
    scans, _ = build_scans(ir)
    rep = stack_args_of(scans["1010:0000"])
    assert not rep.framed and rep.arg_slots == ()
    census = infer_contracts(ir)
    f = census["functions"]["1010:0000"]
    regs = {p["reg"] for p in f["params"]}
    assert "bp" in regs                    # semantic parameter, kept
    assert "sp" not in regs and "ss" not in regs


def test_ss_as_data_segment_vs_frame_access():
    from dos_re.lift.contracts import ss_is_data_segment
    # mov ss:[0x0006], ax ; ret -- the small-model SS==DS idiom: an explicit
    # override on a fixed global, and no stack traffic anywhere
    ir = _ir({"1010:0000": "36 A3 06 00 C3"})
    scans, _ = build_scans(ir)
    assert ss_is_data_segment(scans["1010:0000"]) is True
    # mov ax,[bp+2] ; ret -- SS only by DEFAULT: the frame-access idiom,
    # bp may be a caller-established frame pointer.  NOT a data selector.
    ir = _ir({"1010:0000": "8B 46 02 C3"})
    scans, _ = build_scans(ir)
    assert ss_is_data_segment(scans["1010:0000"]) is False
    # explicit override BUT real stack traffic -> stack and data share the
    # segment; stays machine-classified
    ir = _ir({"1010:0000": "50 36 A3 06 00 58 C3"})
    scans, _ = build_scans(ir)
    assert ss_is_data_segment(scans["1010:0000"]) is False
    # explicit override BUT a CALL: the mechanical composition writes the
    # return address through ss:sp, so the same segment carries frame AND
    # data -- indistinguishable, so it stays machine-classified too
    ir = _ir({"1010:0000": "36 A3 06 00 E8 F8 00 C3", "1010:0100": "C3"})
    scans, _ = build_scans(ir)
    assert ss_is_data_segment(scans["1010:0000"]) is False


def test_pointer_pairs_lodsb():
    # lodsb; ret -> ds:si jointly address memory
    ir = _ir({"1010:0000": "AC C3"})
    scans, _ = build_scans(ir)
    effects, _, _ = compose_effects(ir, scans)
    ins, _ = effects["1010:0000"]
    pairs = pointer_pairs_of(scans["1010:0000"], ins)
    assert pairs.get(("ds", "si")) == 1


def test_infer_contracts_census_end_to_end():
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    census = infer_contracts(ir)
    assert census["summary"]["total"] == 2
    callee = census["functions"]["1010:0100"]
    assert callee["returns"] == ["ax"]
    assert callee["dropped_outputs"] == ["bx"]
    assert callee["returns_status"] == "narrowed"
    assert not callee["refusals"]
    # sp/ss never surface as params or returns
    for f in census["functions"].values():
        regs = {p["reg"] for p in f["params"]}
        assert not (regs & {"sp", "ss"})
        assert not (set(f["returns"]) & {"sp", "ss"})
    assert "1010:0100" in census["promotable"]


def test_infer_contracts_names_attach_as_metadata():
    ir = _ir({"1010:0100": CALLEE})
    census = infer_contracts(ir, names={"1010:0100": "init_pair"})
    notes = census["functions"]["1010:0100"]["notes"]
    assert any(n == "name: init_pair [1010:0100]" for n in notes)


def test_boundary_observer_blocks_narrowing():
    # A boundary head right after the call digests the FULL register bundle
    # against the oracle -- so the callee's bx must NOT be dropped even
    # though the caller's own dataflow overwrites it later.
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    plain = infer_contracts(ir)
    assert plain["functions"]["1010:0100"]["dropped_outputs"] == ["bx"]
    with_park = infer_contracts(
        ir, boundary_addrs=frozenset({(0x1010, 0x0003)}))
    f = with_park["functions"]["1010:0100"]
    assert f["dropped_outputs"] == [] and set(f["returns"]) >= {"ax", "bx"}
    # the park-containing caller widens its inputs (mirrors check_promotable)
    caller = with_park["functions"]["1010:0000"]
    assert "inputs-widened-observer" in caller["notes"]


def test_dispatch_alt_entry_forces_conservative():
    # a dynamic arrival lands mid-callee: its outputs also exit through the
    # dispatcher, so narrowing must not apply
    ir = _ir({"1010:0000": CALLER, "1010:0100": CALLEE})
    census = infer_contracts(
        ir, dispatch_addrs=frozenset({(0x1010, 0x0103)}))
    f = census["functions"]["1010:0100"]
    assert f["returns_status"] == "external-conservative"
    assert f["dropped_outputs"] == []


def test_mixed_return_convention_refuses():
    # jz +1 over retf; ret  (both near and far exits)
    ir = _ir({"1010:0000": "74 01 C3 CB"})
    scans, _ = build_scans(ir)
    rep = stack_args_of(scans["1010:0000"])
    assert rep.ret_kind == "mixed"
    assert "mixed-return-convention" in rep.refusals
    census = infer_contracts(ir)
    reasons = {r["reason"]
               for r in census["functions"]["1010:0000"]["refusals"]}
    assert "stack:mixed-return-convention" in reasons
