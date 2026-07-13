"""PM input demo: round-trip + frame-clock counting (game-free)."""
from dos_re.cpu386 import CPU386, FlatMemory
from dos_re.cpu import HaltExecution
from dos_re.pm_input_demo import (PMInputDemo, FrameClock, INPUT_JSON,
                                  SNAPSHOT_NAME)


def test_demo_roundtrip(tmp_path):
    d = PMInputDemo(0x119D40)
    d.add(3, "key", [True, "space"])
    d.add(3, "key", [False, "space"])
    d.add(5, "mouse", [0.5, 0.9, 0])
    d.total_frames = 8
    p = d.save(tmp_path / "demo.json")
    d2 = PMInputDemo.load(p)
    assert d2.events == d.events
    assert d2.frame_tick_addr == 0x119D40 and d2.total_frames == 8
    assert dict(d2.by_frame())[3] == [("key", [True, "space"]), ("key", [False, "space"])]


def test_demo_bundle_directory(tmp_path):
    """A demo is a bundle directory: manifest + a start-snapshot subdir, and
    ``load``/``snapshot_dir`` resolve it back."""
    bundle = tmp_path / "demo_123"
    (bundle / SNAPSHOT_NAME).mkdir(parents=True)      # stand-in start snapshot
    (bundle / SNAPSHOT_NAME / "pm_state.json").write_text("{}")
    d = PMInputDemo(0x119D40)
    d.snapshot = SNAPSHOT_NAME
    d.add(1, "key", [True, "space"])
    d.total_frames = 4
    d.write_manifest(bundle, status="recording")
    assert (bundle / INPUT_JSON).exists()

    # loading the DIRECTORY reads the manifest and recovers the snapshot name
    d2 = PMInputDemo.load(bundle)
    assert d2.snapshot == SNAPSHOT_NAME and d2.events == d.events
    assert PMInputDemo.snapshot_dir(bundle) == bundle / SNAPSHOT_NAME


def test_cold_start_bundle_has_no_snapshot(tmp_path):
    """A snapshot-less (cold-start) bundle resolves to no start snapshot."""
    bundle = tmp_path / "demo_cold"
    d = PMInputDemo(0x119D40)          # snapshot left None
    d.write_manifest(bundle, status="complete")
    assert PMInputDemo.snapshot_dir(bundle) is None
    # a legacy lone-JSON demo is also treated as snapshot-less
    p = d.save(tmp_path / "legacy.json")
    assert PMInputDemo.snapshot_dir(p) is None


def test_frame_clock_counts_once_per_call():
    # A loop that calls FRAME 4 times then hlt:
    #   mov ecx,4 ; L: call FRAME ; loop L ; hlt      FRAME: ret
    CODE, FRAME = 0x1000, 0x2000
    mem = FlatMemory(size=0x10000)
    # call rel32 to FRAME from 0x1005
    import struct
    disp = FRAME - (0x1005 + 5)
    blob = b"\xB9\x04\x00\x00\x00" + b"\xE8" + struct.pack("<i", disp) + b"\xE2\xF9\xF4"
    mem.load(CODE, blob)
    mem.load(FRAME, b"\xC3")
    cpu = CPU386(mem, eip=CODE, esp=0x8000)
    frames = []
    FrameClock(cpu, FRAME, lambda f: frames.append(f))
    try:
        cpu.run(1000)
    except HaltExecution:
        pass
    assert frames == [0, 1, 2, 3]      # counted once per call, in order
