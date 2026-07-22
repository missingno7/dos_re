"""32-bit lifter end-to-end: scan -> emit -> exec -> differential verify.

Game-free: synthesized functions are lifted mechanically and the generated
hook must survive the strict PM differential verifier (full-machine diff
against the interpreted original) — the same proof a real lift gets.
"""
from __future__ import annotations

import pytest

from dos_re.cpu386 import CPU386, FlatMemory
from dos_re.cpu import HaltExecution
from dos_re.dos4gw import DOS4GWHost
from dos_re.runtime import PMRuntime
from dos_re.lift.cfg32 import scan_function32
from dos_re.lift.emit32 import emit_function32
from dos_re.pm_verification import install_pm_hook_verifier

CODE = 0x1000
FUNC = 0x2000
HELPER = 0x2800
DATA = 0x3000
STACK = 0x8000


def build_rt(func_code: bytes, calls: int = 2) -> PMRuntime:
    mem = FlatMemory(size=0x10000 * 8)
    main = b""
    for _ in range(calls):
        disp = FUNC - (CODE + len(main) + 5)
        main += b"\xE8" + (disp & 0xFFFFFFFF).to_bytes(4, "little")
    main += b"\xF4"
    mem.load(CODE, main)
    mem.load(FUNC, func_code)
    # helper: add eax, 3 ; ret
    mem.load(HELPER, bytes.fromhex("83C003C3"))
    mem.w32(DATA, 7)
    cpu = CPU386(mem, eip=CODE, esp=STACK)
    dos = DOS4GWHost(mem, ".")
    cpu.interrupt_handler = dos.interrupt
    cpu.idt = dos.pm_vectors
    dos._cpu = cpu
    return PMRuntime(image=None, cpu=cpu, dos=dos, mem=mem)


def lift_and_install(rt, entry: int = FUNC, name: str = "lifted"):
    fetch = rt.mem.data.__getitem__
    scan = scan_function32(fetch, entry)
    assert scan.liftable, scan.refusals
    sig = bytes(rt.mem.data[entry:entry + 8])
    src = emit_function32(scan, name, signature=sig)
    ns: dict = {}
    exec(compile(src, f"<lifted 0x{entry:X}>", "exec"), ns)
    rt.cpu.replacement_hooks[entry] = ns[name]
    rt.cpu.hook_names[entry] = name
    return src


def run_to_halt(rt, max_steps=100_000):
    try:
        rt.cpu.run(max_steps)
    except HaltExecution:
        pass


# mov eax,[0x3000] ; add eax,5 ; mov [0x3004],eax ; ret
STRAIGHT = bytes.fromhex("A100300000" "83C005" "A304300000" "C3".replace(" ", ""))

# xor eax,eax ; mov ecx,5 ; L: add eax,ecx ; loop L ; mov [0x3004],eax ; ret
LOOPY = bytes.fromhex("31C0" "B905000000" "01C8" "E2FC" "A304300000" "C3".replace(" ", ""))

# push ebx ; mov ebx,[0x3000] ; lea eax,[ebx+ebx*2+7] ; call HELPER ;
# mov [0x3004],eax ; pop ebx ; ret        (1+6+4 = 11 bytes before the call)
CALLING = (bytes.fromhex("53" "8B1D00300000" "8D445B07".replace(" ", ""))
           + b"\xE8" + ((HELPER - (FUNC + 11 + 5)) & 0xFFFFFFFF).to_bytes(4, "little")
           + bytes.fromhex("A304300000" "5B" "C3".replace(" ", "")))


@pytest.mark.parametrize("func,expect", [
    (STRAIGHT, 12),
    (LOOPY, 15),
    (CALLING, 7 * 3 + 7 + 3),
])
def test_lifted_hook_passes_differential_verify(func, expect):
    rt = build_rt(func)
    lift_and_install(rt)
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == expect


def test_lifted_source_shape():
    rt = build_rt(STRAIGHT)
    src = lift_and_install(rt)
    assert "check_signature" in src
    assert "  # (interpreter fallback)" not in src   # fully native for this one
    assert "cpu.eip = cpu.pop(4)" in src             # the RET terminator


def test_fallback_lines_still_verify():
    # fldz ; fstp dword [0x3004] ; ret — x87 goes through interp_one32.
    func = bytes.fromhex("D9EE" "D91D04300000" "C3".replace(" ", ""))
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == 0              # 0.0f bits


def test_leave_flags_pushad_popad_lift_native_and_verify():
    """leave / pushad / popad / stc-cmc-cld are emitted natively (no interp_one32
    fallback) and survive the strict differential verifier -- part of removing
    the interpreter dependency for a detached build."""
    # push ebp; mov ebp,esp; sub esp,8; mov eax,[0x3000]; inc eax;
    # pushad; stc; cmc; cld; popad; mov [0x3004],eax; leave; ret
    func = bytes.fromhex(
        "55" "89E5" "83EC08" "A100300000" "40" "60" "F9F5FC" "61"
        "A304300000" "C9" "C3")
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32(cpu, 0x" not in src            # no fallback: fully native
    assert "cpu._pusha" in src and "cpu._popa" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == 8                     # 7 + 1


def test_grp3_and_imul_lift_native_and_verify():
    """mul/imul/div/neg/not emit natively (CPU386 _grp3_op / _imul_store, not
    interp_one32) and survive the strict differential verifier."""
    # mov eax,3; mov ecx,4; mul ecx; imul eax,eax,5; xor edx,edx; mov ecx,7;
    # div ecx; neg eax; not eax; mov [0x3004],eax; ret
    func = bytes.fromhex(
        "B803000000" "B904000000" "F7E1" "6BC005" "31D2" "B907000000"
        "F7F1" "F7D8" "F7D0" "A304300000" "C3")
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32(cpu, 0x" not in src            # fully native
    assert "cpu._grp3_op" in src and "cpu._imul_store" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == 7                     # (3*4*5)//7 = 8; -8; ~ = 7


def test_bit_shift_ops_lift_native_and_verify():
    """bt/bts/btr (+grp8) / shld / shrd / bsf / sahf / wait emit natively (CPU386
    _bit_op_do / _shldrd_do primitives, no interp_one32) and match the oracle."""
    # mov eax,0x1200; mov ecx,4; bt eax,ecx; bts eax,ecx; btr eax,9;
    # shld eax,ecx,3; shrd eax,ecx,1; bsf edx,eax; sahf; wait; mov [0x3004],eax; ret
    func = bytes.fromhex(
        "B800120000" "B904000000" "0FA3C8" "0FABC8" "0FBAF009"
        "0FA4C803" "0FACC801" "0FBCD0" "9E" "9B" "A304300000" "C3")
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32(cpu, 0x" not in src            # fully native
    assert "cpu._bit_op_do" in src and "cpu._shldrd_do" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2                 # lifted == interpreted oracle


def test_segment_ops_lift_native_and_verify():
    """push/pop es/ds/fs + mov sreg emit natively (cpu.seg / cpu.set_seg, no
    interp_one32) and match the interpreted oracle."""
    # push ds; push es; mov eax,ds; mov es,ax; pop es; pop ds; push fs; pop fs;
    # mov [0x3004],eax; ret
    func = bytes.fromhex("1E" "06" "8CD8" "8EC0" "07" "1F" "0FA0" "0FA1"
                         "A304300000" "C3")
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32(cpu, 0x" not in src            # fully native
    assert "cpu.set_seg" in src and "cpu.seg[" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2


def test_les_lds_enter_lsl_movcr_lift_native_and_verify():
    """enter / les / lds / lsl / mov cr emit natively (no interp_one32) and match
    the oracle -- leaving only x87 as an interpreter dependency."""
    # enter 8,0; les esi,[0x3010]; lds edi,[0x3010]; lsl eax,eax; mov eax,cr0;
    # mov [0x3004],eax; leave; ret
    func = bytes.fromhex("C8080000" "C43510300000" "C53D10300000" "0F03C0"
                         "0F20C0" "A304300000" "C9" "C3")
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32(cpu, 0x" not in src            # fully native
    assert "cpu.set_seg" in src and "cpu.selector_limits" in src and "cpu.cr" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2


def test_signature_tripwire():
    from dos_re.lift.runtime32 import LiftRuntimeError
    rt = build_rt(STRAIGHT)
    lift_and_install(rt)
    rt.mem.data[FUNC] ^= 0xFF                     # self-modified entry
    with pytest.raises(LiftRuntimeError):
        run_to_halt(rt)


def test_indirect_jump_lifts_as_tail_jump():
    """A switch-style `jmp [table + reg*4]` lifts: the prologue runs native,
    then the computed target is set and control returns to the VM."""
    from dos_re.lift.cfg32 import scan_function32
    from dos_re.lift.emit32 import emit_function32

    # FUNC: mov eax,[0x3000] ; jmp dword [eax*4 + 0x3100]
    # table at 0x3100: [0]=0x2400 [1]=0x2450 ; targets set eax and hlt
    func = bytes.fromhex("A100300000" "FF248500310000".replace(" ", ""))
    rt = build_rt(func)
    mem = rt.mem
    mem.w32(DATA, 1)                     # switch selector = 1
    mem.w32(0x3100 + 0, 0x2400)
    mem.w32(0x3100 + 4, 0x2450)
    mem.load(0x2400, bytes.fromhex("B8AA000000F4"))   # mov eax,0xAA ; hlt
    mem.load(0x2450, bytes.fromhex("B8BB000000F4"))   # mov eax,0xBB ; hlt
    scan = scan_function32(mem.data.__getitem__, FUNC)
    assert scan.liftable, scan.refusals
    src = emit_function32(scan, "sw", signature=bytes(mem.data[FUNC:FUNC + 8]))
    assert "cpu.eip = mem.r32(_o)" in src
    ns = {}
    exec(compile(src, "<sw>", "exec"), ns)
    rt.cpu.replacement_hooks[FUNC] = ns["sw"]
    rt.cpu.hook_names[FUNC] = "sw"
    # main: call FUNC (which tail-jumps to case 1 -> eax=0xBB)
    run_to_halt(rt)
    assert rt.cpu.r[0] == 0xBB           # eax == case-1 result
