"""Self-modifying / runtime-patched code must REFUSE to lift, loudly.

Found the hard way (SkyRoads' LZS decoder, 1010:66E6): the routine reads each
compressed file's header and writes the bit-width immediates INTO ITS OWN BODY
(``mov cs:[imm16], al``) before decoding.  A literal lift froze the widths one
snapshot happened to hold, decoded every other file with them, and corrupted
the whole startup allocation chain -- silently.  The lifter's job is to make
that IMPOSSIBLE: statically-visible code writes refuse the PATCHED function at
census time, turning silent corruption into an explicit not-liftable entry.
"""
from __future__ import annotations

from dos_re.lift.cfg import cs_direct_store_target, scan_function
from dos_re.lift.decode import decode_one
from dos_re.lift.irgen_core import build_document


def _fetch(code: bytes, base: int = 0):
    def fetch(off: int) -> int:
        i = off - base
        return code[i] if 0 <= i < len(code) else 0x90   # nop padding
    return fetch


def test_cs_direct_store_forms_are_detected() -> None:
    cases = [
        (bytes.fromhex("2ea22967"), 0x6729),        # mov cs:[6729], al
        (bytes.fromhex("2ea32967"), 0x6729),        # mov cs:[6729], ax
        (bytes.fromhex("2e88160500"), 0x0005),      # mov cs:[0005], dl
        (bytes.fromhex("2ec606050042"), 0x0005),    # mov byte cs:[0005], 0x42
        (bytes.fromhex("2eff060500"), 0x0005),      # inc word cs:[0005]
    ]
    for raw, want in cases:
        inst = decode_one(_fetch(raw), 0)
        assert cs_direct_store_target(inst) == want, raw.hex()


def test_reads_and_non_cs_stores_are_not_flagged() -> None:
    cases = [
        bytes.fromhex("2ea02967"),        # mov al, cs:[6729] -- a READ
        bytes.fromhex("2e8b1e0500"),      # mov bx, cs:[0005] -- a READ
        bytes.fromhex("a22967"),          # mov [6729], al -- DS, not CS
        bytes.fromhex("2e803e050007"),    # cmp byte cs:[0005], 7 -- a READ
        bytes.fromhex("2e88560a"),        # mov cs:[bp+0a], dl -- not direct
    ]
    for raw in cases:
        inst = decode_one(_fetch(raw), 0)
        assert cs_direct_store_target(inst) is None, raw.hex()


def test_scan_refuses_a_function_that_patches_its_own_body() -> None:
    # 0000: mov cs:[0005], al   (2e a2 05 00)  -- patches the imm byte below
    # 0004: push imm8           (6a XX)        <- byte 0005 is the operand
    # 0006: ret
    code = bytes.fromhex("2ea205006a00c3")
    scan = scan_function(_fetch(code), 0)
    assert not scan.liftable
    assert any(r.reason == "self-modifying" for r in scan.refusals)


def test_scan_allows_a_cs_write_that_lands_elsewhere() -> None:
    # The same store, but targeting 0x4000 -- far outside this function.  The
    # scan itself stays liftable; whether 0x4000 is another censused function
    # is the document-level pass's call.
    code = bytes.fromhex("2ea200406a00c3")
    scan = scan_function(_fetch(code), 0)
    assert scan.liftable
    assert scan.cs_store_targets == [(0, 0x4000)]


def test_build_document_refuses_the_patched_victim_function() -> None:
    # writer @0000: mov cs:[0101], al ; ret     -- patches the victim's imm
    # victim @0100: push imm8 ; ret             -- byte 0101 is its operand
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea20101c3")
    image[0x100:0x103] = bytes.fromhex("6a00c3")

    def fetch_for(cs: int):
        return _fetch(bytes(image))

    doc = build_document([(0x1010, 0x0000), (0x1010, 0x0100)],
                         fetch_for=fetch_for,
                         provenance={}, notice="test")
    victim = doc["functions"]["1010:0100"]
    assert victim["liftable"] is False
    reasons = {u["reason"] for u in doc["unsupported"]
               if u["entry"] == "1010:0100"}
    assert "code-patched-at-runtime" in reasons
    # the writer itself is untouched by the victim's refusal
    assert doc["functions"]["1010:0000"]["liftable"] is True
