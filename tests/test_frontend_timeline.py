"""Unit tests for the generic front-end timeline core (dos_re.frontend_timeline).

``rgb_sha`` is duck-typed (ndarray ``.tobytes()`` or bytes-like), so these tests exercise both branches with
plain ``bytes`` and a tiny ``.tobytes()`` stand-in — no numpy needed for what is being proven here."""
from __future__ import annotations

import hashlib

from dos_re.frontend_timeline import (FrameRecord, capture, collapse, diff_pixels, diff_sequence,
                                      format_sequence, rgb_sha)


class _FakeArr:
    """Stands in for a numpy array: ``rgb_sha`` hashes ``.tobytes()`` when present (the array path)."""
    def __init__(self, buf: bytes):
        self._buf = buf

    def tobytes(self) -> bytes:
        return self._buf


def _mk(screens):
    """A timeline from a list of (screen, rgb_sha) — rgb_sha stands in for the pixel digest."""
    return [FrameRecord(i, s, h) for i, (s, h) in enumerate(screens)]


def test_rgb_sha_stable_and_blank():
    zeros = bytes(4 * 4 * 3)                                  # an all-zero "RGB" frame as raw bytes
    assert rgb_sha(zeros) == rgb_sha(bytes(4 * 4 * 3))        # deterministic + stable
    assert rgb_sha(b"\x01" + zeros[1:]) != rgb_sha(zeros)     # one changed pixel -> different digest
    assert rgb_sha(None) == ""                                # blank / no frame
    # the array branch (hasattr .tobytes): same bytes -> same digest as the bytes path, == a raw sha1
    assert rgb_sha(_FakeArr(zeros)) == rgb_sha(zeros) == hashlib.sha1(zeros).hexdigest()


def test_capture_stops_on_none_and_maxframes():
    seq = ["a", "a", "b"]
    got = capture(lambda i: (seq[i], f"h{i}") if i < len(seq) else None, max_frames=99)
    assert [r.screen for r in got] == ["a", "a", "b"]
    got2 = capture(lambda i: ("x", "h"), max_frames=3)      # never returns None -> capped
    assert len(got2) == 3


def test_collapse_runs():
    runs = collapse(_mk([("oldies", "0"), ("oldies", "1"), ("13h:TITUS", "2"), ("13h:MENU", "3"), ("13h:MENU", "4")]))
    assert [(r.screen, r.count, r.start) for r in runs] == [
        ("oldies", 2, 0), ("13h:TITUS", 1, 2), ("13h:MENU", 2, 3)]
    assert format_sequence(runs) == "oldiesx2 -> 13h:TITUSx1 -> 13h:MENUx2"


def test_diff_sequence_order_match_ignoring_duration():
    ref = collapse(_mk([("a", "")] * 3 + [("b", "")] * 5))
    cand = collapse(_mk([("a", "")] * 10 + [("b", "")] * 1))     # same order, very different durations
    assert diff_sequence(ref, cand, duration_tolerance=None).ok            # durations ignored -> OK
    d = diff_sequence(ref, cand, duration_tolerance=2)                     # durations enforced -> diverge on run 0
    assert not d.ok and d.index == 0 and "frames" in d.reason


def test_diff_sequence_screen_mismatch_and_extra():
    ref = collapse(_mk([("a", ""), ("b", "")]))
    # this is the expert-eater CLASS of bug: native inserts carte+level before the wall
    bug = collapse(_mk([("a", ""), ("carte", ""), ("level", ""), ("b", "")]))
    d = diff_sequence(ref, bug, duration_tolerance=None)
    assert not d.ok and d.index == 1 and d.b.screen == "carte"
    # candidate shorter than reference -> missing screen
    d2 = diff_sequence(ref, collapse(_mk([("a", "")])), duration_tolerance=None)
    assert not d2.ok and d2.reason == "missing screen"


def test_filter_runs_drops_transitions_and_merges():
    from dos_re.frontend_timeline import filter_runs
    runs = collapse(_mk([("oldies", "")] * 3 + [("loading", "")] * 2 + [("title", "")] * 4
                        + [("blanked", "")] + [("title", "")] * 2 + [("menu", "")]))
    got = filter_runs(runs, ignore={"loading", "blanked"})
    # the blanked run split 'title' in two — filtering merges them back; counts are preserved
    assert [(r.screen, r.count) for r in got] == [("oldies", 3), ("title", 6), ("menu", 1)]
    # nothing ignored -> unchanged
    assert filter_runs(runs) == runs


def test_diff_pixels_first_divergence_and_length():
    ref = _mk([("a", "h0"), ("a", "h1"), ("b", "h2")])
    same = _mk([("a", "h0"), ("a", "h1"), ("b", "h2")])
    assert diff_pixels(ref, same).ok
    drift = _mk([("a", "h0"), ("a", "hX"), ("b", "h2")])
    d = diff_pixels(ref, drift)
    assert not d.ok and d.frame == 1 and d.sha_ref == "h1" and d.sha_cand == "hX"
    longer = _mk([("a", "h0"), ("a", "h1"), ("b", "h2"), ("b", "h3")])
    dl = diff_pixels(ref, longer)
    assert not dl.ok and dl.frame == 3


def test_pack_and_diff_fields():
    from dos_re.frontend_timeline import diff_fields, pack_fields
    fields = (("level", 0x10, 1), ("score", 0x20, 4))
    data = bytearray(0x40); data[0x10] = 7; data[0x20:0x24] = (1234).to_bytes(4, "little")
    w1 = pack_fields(data, fields)
    data2 = bytearray(data); data2[0x10] = 8
    w2 = pack_fields(data2, fields)
    assert w1 != w2
    d = diff_fields(w1, w2, fields)
    assert d == ["level: ref=07 cand=08"]                      # only the changed field, named
    assert diff_fields(w1, w1, fields) == []


def test_input_segments_and_segmented_feeder():
    from dos_re.frontend_timeline import ScreenRun, SegmentedInput, input_segments
    # reference: screen A for 3 frames, B for 2, C for 3 (8 frames of input i0..i7)
    runs = [ScreenRun("A", 0, 3), ScreenRun("B", 3, 2), ScreenRun("C", 5, 3)]
    inputs = [bytes([i]) for i in range(8)]
    segs = input_segments(runs, inputs, 8)
    assert [(s, [x[0] for x in frames]) for s, frames in segs] == [
        ("A", [0, 1, 2]), ("B", [3, 4]), ("C", [5, 6, 7])]
    # candidate: finishes A in ONE frame (timed screen), then B for 3 frames (longer than recorded), then C
    feeder = SegmentedInput(segs, blank=b"\xff")
    out = [feeder.next("A")[0]]                                # A frame 0 -> input 0
    out.append(feeder.next("B")[0])                            # screen advanced -> B segment from its start (3)
    out.append(feeder.next("B")[0])                            # 4
    out.append(feeder.next("B")[0])                            # B exhausted -> blank
    out.append(feeder.next("C")[0])                            # C segment start (5)
    assert out == [0, 3, 4, 0xFF, 5]


def test_diff_offsets_and_spread_beyond():
    from dos_re.frontend_timeline import diff_offsets, spread_beyond
    a = bytes([0, 1, 2, 3, 4]); b = bytes([0, 9, 2, 8, 4])
    owned = set(diff_offsets(a, b))
    assert owned == {1, 3}
    # a tick later: candidate diverges at a NEW offset -> the leak is localized
    a2 = bytes([0, 1, 5, 3, 4]); b2 = bytes([0, 9, 6, 8, 4])
    assert spread_beyond(a2, b2, owned) == [2]
    assert spread_beyond(a2, b2, owned | {2}) == []
