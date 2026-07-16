"""dos_re.lift.emit: differential proof that lifted Python == interpreted ASM.

The house style, applied to the lifter itself: hand-assemble a function, lift
it, then run the ORIGINAL through the interpreter and the LIFTED hook from the
same randomized start state and diff registers + flags + full memory. Any
emitter bug shows up as a divergence.

Synthetic code only (game-free tests rule).
"""
from __future__ import annotations

import random

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.memory import Memory

CS = 0x1000
ENTRY = 0x0100
RET_IP = 0xBEEF


def _lift(code: bytes, entry: int = ENTRY, **kw):
    fetch = lambda off: code[(off - entry) & 0xFFFF] if 0 <= (off - entry) < len(code) else 0x90
    scan = scan_function(fetch, entry)
    assert scan.liftable, [(f"{r.ip:04X}", r.reason, r.detail) for r in scan.refusals]
    src = emit_function(scan, CS, "lifted", signature=code[:8], **kw)
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102 — that's the point
    return ns["lifted"], scan, src


def _make_cpu(code: bytes, state: CPUState, data: bytes = b"", entry: int = ENTRY) -> CPU8086:
    mem = Memory()
    mem.load(CS, entry, code)
    if data:
        mem.load(0x4000, 0x0000, data)
    cpu = CPU8086(mem, state)
    cpu.trace_enabled = False
    cpu.push(RET_IP)
    return cpu


def _rand_state(rng: random.Random, entry: int = ENTRY) -> CPUState:
    return CPUState(
        ax=rng.randrange(0x10000), bx=rng.randrange(0x10000),
        cx=rng.randrange(1, 8), dx=rng.randrange(0x10000),
        si=rng.randrange(0x100), di=rng.randrange(0x100), bp=rng.randrange(0x100),
        sp=0x2000, cs=CS, ip=entry, ds=0x4000, es=0x4000, ss=0x3000,
        flags=(rng.getrandbits(16) & 0x0CD5) | 0x0202,
    )


def _run_interpreted(cpu: CPU8086, limit: int = 20000) -> None:
    for _ in range(limit):
        if (cpu.s.cs, cpu.s.ip) == (CS, RET_IP):
            return
        cpu.step()
    raise AssertionError("interpreted run did not reach the return address")


def _assert_equivalent(code: bytes, *, cases: int = 60, data_len: int = 0, entry: int = ENTRY,
                       seed: int = 0x11FE, **kw) -> str:
    # Virtual-time preservation is part of the equivalence contract: unless a
    # test opts out, every lift is emitted counting and must advance
    # instruction_count EXACTLY as the interpreter does (PIT reads and every
    # other time-derived observable depend on it).
    kw.setdefault("count_instructions", True)
    check_time = kw["count_instructions"]
    lifted, _scan, src = _lift(code, entry, **kw)
    rng = random.Random(seed)
    for case in range(cases):
        state = _rand_state(rng, entry)
        data = bytes(rng.randrange(256) for _ in range(data_len)) if data_len else b""

        asm = _make_cpu(code, CPUState(**{k: getattr(state, k) for k in state.__slots__}),
                        data, entry)
        hook = _make_cpu(code, CPUState(**{k: getattr(state, k) for k in state.__slots__}),
                         data, entry)
        _run_interpreted(asm)
        lifted(hook)

        assert (hook.s.cs, hook.s.ip) == (CS, RET_IP), f"case {case}: lifted did not return"
        assert asm.s.snapshot() == hook.s.snapshot(), f"case {case} registers/flags\n{src}"
        assert asm.mem.data == hook.mem.data, f"case {case} memory\n{src}"
        if check_time:
            assert asm.instruction_count == hook.instruction_count, \
                f"case {case} virtual time: interpreted {asm.instruction_count} " \
                f"!= lifted {hook.instruction_count}\n{src}"
    return src


# --- the emitter's native opcode coverage --------------------------------------

def test_alu_reg_rm_and_flags():
    code = bytes.fromhex(
        "01D8"    # add ax, bx
        "29D8"    # sub ax, bx
        "11D8"    # adc ax, bx
        "19D8"    # sbb ax, bx
        "21D8"    # and ax, bx
        "09D8"    # or  ax, bx
        "31D8"    # xor ax, bx
        "39D8"    # cmp ax, bx
        "00E0"    # add al, ah
        "38E0"    # cmp al, ah
        "C3")
    _assert_equivalent(code)


def test_alu_immediate_forms():
    code = bytes.fromhex(
        "053412"                        # add ax, 0x1234
        "2D3412"                        # sub ax, 0x1234
        "3C7F"                          # cmp al, 0x7F
        "81C33412"                      # add bx, 0x1234
        "83EB05"                        # sub bx, 5 (imm8 sign-extended)
        "83C3FE"                        # add bx, -2
        "80F37F"                        # xor bl, 0x7F
        "C3")
    _assert_equivalent(code)


def test_mov_family_and_memory_operands():
    code = bytes.fromhex(
        "B83412"      # mov ax, 0x1234
        "BB0400"      # mov bx, 4
        "8907"        # mov [bx], ax
        "8B0F"        # mov cx, [bx]
        "8A27"        # mov ah, [bx]
        "884701"      # mov [bx+1], al
        "8B871000"    # mov ax, [bx+0x0010]
        "8B0E2000"    # mov cx, [0x0020]
        "A12200"      # mov ax, [0x0022]
        "A32400"      # mov [0x0024], ax
        "C7070500"    # mov word [bx], 5
        "C6470141"    # mov byte [bx+1], 0x41
        "C3")
    _assert_equivalent(code, data_len=0x80)


def test_segment_override_and_sreg_moves():
    code = bytes.fromhex(
        "268B07"      # mov ax, es:[bx]
        "8CC1"        # mov cx, es
        "8ED9"        # mov ds, cx
        "8CD8"        # mov ax, ds
        "C3")
    _assert_equivalent(code, data_len=0x80)


def test_inc_dec_push_pop_xchg_lea():
    code = bytes.fromhex(
        "40" "48" "43" "4B"      # inc ax, dec ax, inc bx, dec bx
        "50" "53" "5B" "58"      # push ax, push bx, pop bx, pop ax
        "FEC4"                   # inc ah
        "FECC"                   # dec ah
        "FF07"                   # inc word [bx]
        "FF0F"                   # dec word [bx]
        "87D8"                   # xchg ax, bx
        "91"                     # xchg ax, cx
        "8D5F02"                 # lea bx, [bx+2]
        "90"                     # nop
        "1E" "07"                # push ds, pop es
        "6A05" "58"              # push 5, pop ax
        "683412" "5B"            # push 0x1234, pop bx
        "53" "8F063000"          # push bx ; pop word [0x0030]  (balanced)
        "C3")
    _assert_equivalent(code, data_len=0x80)


def test_les_lds_load_far_pointer_native():
    # les/lds read a 16:16 pointer from memory into a reg + ES/DS.  Keep the
    # loaded segment unused afterwards (ret immediately) so the poisoned seg
    # from random data never drives a downstream access — the snapshot diff
    # still proves reg + ES/DS were loaded byte-exact.
    code = bytes.fromhex(
        "BB1000"      # mov bx, 0x0010
        "C407"        # les ax, [bx]      -> ax=word[0x10], es=word[0x12]
        "C55F04"      # lds bx, [bx+4]    -> bx=word[0x14], ds=word[0x16]
        "C3")
    src = _assert_equivalent(code, data_len=0x80)
    assert "# (interpreter fallback)" not in src
    assert "s.es = _seg" in src and "s.ds = _seg" in src


def test_shifts_rotates_and_misc():
    code = bytes.fromhex(
        "D1E0"    # shl ax, 1
        "D1E8"    # shr ax, 1
        "D1D0"    # rcl ax, 1
        "D1C0"    # rol ax, 1
        "C1E003"  # shl ax, 3
        "D3E3"    # shl bx, cl
        "98"      # cbw
        "99"      # cwd
        "9C" "9D" # pushf/popf
        "D7"      # xlat
        "C3")
    _assert_equivalent(code, data_len=0x200)


def test_test_instruction():
    code = bytes.fromhex("85D8" "84E0" "A93412" "A87F" "C3")
    _assert_equivalent(code)


def test_string_ops_native():
    code = bytes.fromhex(
        "FC"        # cld
        "AC"        # lodsb
        "AA"        # stosb
        "AD"        # lodsw
        "AB"        # stosw
        "A4"        # movsb
        "A5"        # movsw
        "A6"        # cmpsb
        "AE"        # scasb
        "C3")
    src = _assert_equivalent(code, data_len=0x200)
    # cld and every string op are native now.
    assert src.count("cpu.string_op(") == 8
    assert "# (interpreter fallback)" not in src


def test_rep_string_ops_native():
    code = bytes.fromhex(
        "FC"        # cld
        "B90500"    # mov cx, 5
        "F3A4"      # rep movsb
        "B90300"    # mov cx, 3
        "F3AB"      # rep stosw
        "C3")
    _assert_equivalent(code, data_len=0x200)


def test_string_op_with_segment_override():
    code = bytes.fromhex("FC" "26AC" "AA" "C3")   # cld ; lodsb es:[si] ; stosb
    src = _assert_equivalent(code, data_len=0x200)
    assert "cpu.string_op(0xAC, None, 'es')" in src


def test_flag_ops_native_and_exact():
    # clc stc cld std cmc cli sti — all native now, no interpreter fallback.
    code = bytes.fromhex("F8" "F9" "FC" "FD" "F5" "FA" "FB" "C3")
    src = _assert_equivalent(code)
    assert "# (interpreter fallback)" not in src
    assert "cpu.set_flag(CF" in src and "cpu.set_flag(DF" in src
    assert "cpu.set_flag(IF" in src


def test_in_out_native_route_through_ports():
    # in al,60h ; out 61h,al ; in ax,dx ; out dx,ax ; in al,dx ; out 20h,al
    code = bytes.fromhex("E460" "E661" "ED" "EF" "EC" "E620" "C3")
    reads: list = []
    writes: list = []

    def reader(cpu, port, bits):
        reads.append((port, bits))
        return 0x5A5A

    def writer(cpu, port, value, bits):
        writes.append((port, value, bits))

    lifted, _scan, src = _lift(code)
    assert "# (interpreter fallback)" not in src
    st = _rand_state(random.Random(0x104))
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    for c in (asm, hook):
        c.port_reader = reader
        c.port_writer = writer
    _run_interpreted(asm)
    a_reads, a_writes = list(reads), list(writes)
    reads.clear(); writes.clear()
    lifted(hook)
    # Both paths hit the ports with identical arguments, and end byte-exact.
    assert reads == a_reads
    assert writes == a_writes
    assert asm.s.snapshot() == hook.s.snapshot()
    assert asm.mem.data == hook.mem.data


def test_grp3_mul_div_neg_not_test_native():
    code = bytes.fromhex(
        "B90300"   # mov cx, 3
        "F7E1"     # mul cx        (16-bit unsigned)
        "F7F1"     # div cx        (16-bit unsigned)
        "F7E9"     # imul cx       (16-bit signed)
        "F7F9"     # idiv cx       (16-bit signed)
        "F6E1"     # mul cl        (8-bit unsigned)
        "F6F1"     # div cl        (8-bit unsigned)
        "F6E9"     # imul cl       (8-bit signed)
        "F6F9"     # idiv cl       (8-bit signed)
        "F7D8"     # neg ax
        "F7D0"     # not ax
        "F6D4"     # not ah
        "F7C13412" # test cx, 0x1234
        "F6C37F"   # test bl, 0x7F
        "C3")
    src = _assert_equivalent(code)
    assert "# (interpreter fallback)" not in src


def test_grp3_memory_operands_native():
    # r/m memory forms for neg/not/mul/imul/test at both widths and with a
    # displacement (div/idiv on random memory would raise on a zero divisor —
    # divide-by-zero is covered separately).
    code = bytes.fromhex(
        "BB0400"   # mov bx, 4
        "F71F"     # neg  word [bx]
        "F717"     # not  word [bx]
        "F627"     # mul  byte [bx]
        "F76F10"   # imul word [bx+0x10]
        "F65710"   # not  byte [bx+0x10]
        "F707FF00" # test word [bx], 0x00FF
        "F6472034" # test byte [bx+0x20], 0x34
        "C3")
    src = _assert_equivalent(code, data_len=0x80, seed=0x9911)
    assert "# (interpreter fallback)" not in src


def test_grp3_divide_by_zero_raises_on_both_paths():
    # mov cx, 0 ; div cx  -> ZeroDivisionError both interpreted and lifted.
    for grp3 in ("F7F1", "F7F9", "F6F1", "F6F9"):   # div/idiv cx, div/idiv cl
        code = bytes.fromhex("31C9" + grp3 + "C3")   # xor cx,cx ; <grp3> ; ret
        lifted, _scan, _src = _lift(code)
        st = _rand_state(random.Random(0x0))
        asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
        hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
        with pytest.raises(ZeroDivisionError):
            _run_interpreted(asm)
        with pytest.raises(ZeroDivisionError):
            lifted(hook)


# --- control flow ---------------------------------------------------------------

def test_conditional_branches_and_loop():
    code = bytes.fromhex(
        "31C0"      # xor ax, ax
        "01D8"      # add ax, bx      <- loop body (0x0102)
        "E2FC"      # loop 0x0102
        "7401"      # jz +1
        "40"        # inc ax
        "C3")       # ret
    _assert_equivalent(code)


def test_jcxz_and_loopz_loopnz():
    for hexbytes in ("E301" "40" "C3", "E101" "40" "C3", "E001" "40" "C3"):
        _assert_equivalent(bytes.fromhex(hexbytes))


def test_forward_jmp_and_diamond():
    code = bytes.fromhex(
        "39D8"      # cmp ax, bx
        "7304"      # jnb +4  -> 0x0108
        "01D8"      # add ax, bx
        "EB02"      # jmp +2  -> 0x010A
        "29D8"      # sub ax, bx   (0x0108)
        "C3")       # ret          (0x010A)
    _assert_equivalent(code)


def test_ret_imm_pops_arguments():
    code = bytes.fromhex("50" "58" "C20200")   # push ax; pop ax; ret 2
    lifted, _s, _src = _lift(code)
    rng = random.Random(7)
    st = _rand_state(rng)
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    _run_interpreted(asm)
    lifted(hook)
    assert asm.s.snapshot() == hook.s.snapshot()


def test_near_call_runs_callee_through_the_vm():
    # 0100: call 0x0104 ; 0103: ret ; 0104: inc ax ; 0105: ret
    code = bytes.fromhex("E80100" "C3" "40" "C3")
    _assert_equivalent(code)


def test_call_composes_with_an_installed_hook_on_the_callee():
    """A lifted function's CALL dispatches whatever hook exists at the callee —
    lifting order never matters (design §1.3)."""
    code = bytes.fromhex("E80100" "C3" "40" "C3")
    lifted, _s, _src = _lift(code)
    st = _rand_state(random.Random(3))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    fired = []

    def callee_hook(cpu):
        fired.append(True)
        cpu.s.ax = (cpu.s.ax + 0x100) & 0xFFFF   # deliberately different from `inc ax`
        cpu.s.ip = cpu.pop()

    hook.replacement_hooks[(CS, 0x0104)] = callee_hook
    lifted(hook)
    assert fired, "the callee hook never ran"
    assert hook.s.ax == (st.ax + 0x100) & 0xFFFF


def test_indirect_near_call():
    # 0100: mov word [0x0030],0x010B ; 0106: call [0x0030] ; 010A: ret ; 010B: inc ax ; ret
    code = bytes.fromhex("C706300 00B01".replace(" ", "") + "FF163000" "C3" "40" "C3")
    _assert_equivalent(code, data_len=0x80)


def test_smc_guard_fails_loud_when_entry_bytes_change():
    """The generated guard uses the framework's fail-fast signature check: if the
    lifted region was patched at runtime, the hook refuses to run rather than
    executing a replacement for code that is no longer there."""
    code = bytes.fromhex("40" "C3")
    lifted, _s, src = _lift(code)
    assert "self_disable_if_patched" in src
    st = _rand_state(random.Random(1))
    cpu = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    cpu.mem.wb(CS, ENTRY, 0x90)               # patch the region: nop out `inc ax`
    with pytest.raises(RuntimeError, match="runtime-patched code"):
        lifted(cpu)

    fresh = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    lifted(fresh)                              # unpatched: runs normally
    assert fresh.s.ax == (st.ax + 1) & 0xFFFF


def test_instruction_count_option_reproduces_the_asm_clock():
    # Virtual-time preservation: the counted lift IS the ASM clock — exact on
    # a direct call (a linked caller), and exact under step() dispatch because
    # the emitted module marks the function owns_time and step() skips its
    # dispatch +1.  Everything the machine models derive from
    # instruction_count (PIT reads above all) depends on this.
    code = bytes.fromhex("40" "43" "01D8" "C3")     # 4 instructions
    lifted, _s, _src = _lift(code, count_instructions=True)
    assert getattr(lifted, "owns_time", False) is True
    st = _rand_state(random.Random(5))
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    _run_interpreted(asm)
    lifted(hook)                              # direct call (the linked-call path)
    assert hook.instruction_count == asm.instruction_count == 4

    # step()-dispatch path: install as a replacement hook and step once.
    stepped = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    stepped.replacement_hooks[(CS, ENTRY)] = lifted
    stepped.step()
    assert stepped.instruction_count == 4     # owns_time: no dispatch +1

    # A non-counting hook still gets step()'s classic +1.
    plain, _s2, _src2 = _lift(code, count_instructions=False)
    assert not getattr(plain, "owns_time", False)
    legacy = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    legacy.replacement_hooks[(CS, ENTRY)] = plain
    legacy.step()
    assert legacy.instruction_count == 1


def test_runaway_internal_loop_fails_loud_not_hangs():
    """A lifted function runs synchronously; an unbounded internal spin (e.g. a
    hardware-wait poll) would hang the generated dispatch loop, so it is bounded
    and raises instead. Here: `jmp $` — an infinite self-loop with no exit."""
    from dos_re.lift.runtime import LiftRuntimeError
    # 0100: nop ; 0101: jmp 0x0100  (never returns)
    code = bytes.fromhex("90" "EBFD")
    fetch = lambda off: code[(off - ENTRY)] if 0 <= off - ENTRY < len(code) else 0x90
    scan = scan_function(fetch, ENTRY)
    # scan refuses (no exit); force emission to prove the guard by lifting a
    # function that DOES exit but whose lifted form we then trap.  Instead use a
    # bounded emitter directly with a tiny MAX via a self-branching diamond.
    # Simpler: a conditional self-loop that the guard catches when it never exits.
    code = bytes.fromhex("7DFE" "C3")   # 0100: jge 0x0100 (loops while SF==OF) ; ret
    scan = scan_function(lambda off: code[(off - ENTRY)] if 0 <= off - ENTRY < len(code) else 0x90,
                         ENTRY)
    src = emit_function(scan, CS, "lifted", signature=code[:4])
    src = src.replace("MAX_ITERATIONS = ", "MAX_ITERATIONS = 50  # ")  # shrink for the test
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102
    st = _rand_state(random.Random(9))
    st.flags &= ~0x0880           # SF=0, OF=0 -> jge taken -> infinite self-loop
    cpu = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    with pytest.raises(LiftRuntimeError, match="MAX_ITERATIONS"):
        ns["lifted"](cpu)


def test_emitted_source_carries_disassembly_comments():
    _lifted, _scan, src = _lift(bytes.fromhex("01D8" "C3"))
    assert "AUTOGENERATED by dos_re.lift" in src
    assert "1000:0100" in src and "01d8" in src
    assert "def lifted(cpu):" in src


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_randomized_mixed_function(seed):
    """A denser function mixing memory, flags, branches and a call."""
    code = bytes.fromhex(
        "50"          # push ax
        "8B0E3000"    # mov cx, [0x0030]
        "83E103"      # and cx, 3
        "E30A"        # jcxz +10 -> 0x0114
        "8B1E3200"    # mov bx, [0x0032]   (0x010A)
        "01D8"        # add ax, bx
        "D1E0"        # shl ax, 1
        "E2F6"        # loop 0x010A
        "58"          # pop ax             (0x0114)
        "E80100"      # call 0x0119
        "C3"          # ret
        "F7D8"        # neg ax             (0x0119)
        "C3")         # ret
    _assert_equivalent(code, cases=40, data_len=0x80, seed=seed)


def test_entry_fallback_does_not_recurse_into_its_own_hook():
    """A function whose ENTRY instruction is an interpreter fallback must not
    re-dispatch its own replacement hook through interp_one (infinite
    recursion — found by the first Win16 lift: Borland/MS C prologues enter
    via `enter`, a fallback op).  interp_one suppresses the hook at exactly
    that CS:IP for its one step."""
    code = bytes.fromhex(
        "27"          # daa                (fallback op at the ENTRY)
        "01D8"        # add ax, bx
        "C3")         # ret
    lifted, _scan, _src = _lift(code)
    rng = random.Random(0xE117)
    st = _rand_state(rng)
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    # install the lift AT ITS OWN ENTRY, exactly as liftverify does
    hook.replacement_hooks[(CS, ENTRY)] = lambda cpu: lifted(cpu)
    _run_interpreted(asm)
    hook.step()                       # dispatches the hook -> runs the lift
    assert (hook.s.cs, hook.s.ip) == (CS, RET_IP)
    assert (CS, ENTRY) in hook.replacement_hooks    # hook restored after the step
    assert hook.s.ax == asm.s.ax and hook.s.flags == asm.s.flags


def test_pascal_callee_ret_n_terminates_the_emulated_call():
    """`ret n` / `retf n` (pascal — every Win16 API and most Win16 game code)
    pops the args too, so the callee returns with SP ABOVE the pre-call mark.
    The emulated call must recognize that as the return instead of running
    away through the rest of the program (found by the first Win16 lift:
    IsWindowVisible's `retf 2` never matched the strict-SP done())."""
    code = bytes.fromhex(
        "B80200"      # mov ax, 2          (0x0100)
        "50"          # push ax            (the argument)
        "E80300"      # call 0x010A
        "01D8"        # add ax, bx         (0x0108, after the call)
        "C3"          # ret                (0x010A-1... exits the function)
        "5A"          # pop dx             (0x010A: callee — pop ret addr? no:)
        )
    # hand-build precisely: caller pushes arg, calls; callee does
    #   mov ax, [sp+2] equivalent work then RET 2 (cleans the arg).
    code = bytes.fromhex(
        "B80700"      # 0100: mov ax, 7
        "50"          # 0103: push ax          (arg)
        "E8020000"    # won't use — lengths matter; rebuild below
    )
    code = bytes.fromhex(
        "B80700"      # 0100: mov ax, 7
        "50"          # 0103: push ax           arg for the callee
        "E80400"      # 0104: call 0x010B
        "050100"      # 0107: add ax, 1         (post-return)
        "C3"          # 010A: ret               function exit
        "8BDC"        # 010B: mov bx, sp        callee
        "368B5F02"    # 010D: mov bx, ss:[bx+2] read the arg
        "03C3"        # 0111: add ax, bx
        "C20200")     # 0113: ret 2             pascal: pops the arg too
    lifted, _scan, _src = _lift(code)
    rng = random.Random(0x9A5C)
    st = _rand_state(rng)
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    _run_interpreted(asm)
    lifted(hook)
    assert (hook.s.cs, hook.s.ip) == (CS, RET_IP)
    assert hook.s.ax == asm.s.ax == ((7 + 7 + 1) & 0xFFFF)
    assert hook.s.sp == asm.s.sp


def test_entry_block_dispatches_first_even_when_not_the_lowest_leader():
    """A region can contain a branch target BELOW the entry; block_leaders()
    sorts by address, so the entry is not block 0.  The dispatch loop must
    start at the ENTRY block — hardcoding 0 executed the lowest-address block
    first (found by the Lemmings pilot's whole-program census)."""
    base = 0x0100
    code = bytes.fromhex(
        "40"        # 0100: inc ax      backward target, below the entry
        "C3"        # 0101: ret
        "4B"        # 0102: dec bx      ENTRY
        "75FB"      # 0103: jnz 0100
        "C3")       # 0105: ret
    fetch = lambda off: code[off - base] if 0 <= off - base < len(code) else 0x90
    from dos_re.lift.cfg import scan_function as _scan_fn
    scan = _scan_fn(fetch, 0x0102)
    assert scan.liftable
    assert scan.block_leaders()[0] != 0x0102     # the premise: entry isn't block 0
    src = emit_function(scan, CS, "lifted", signature=code[2:])
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102
    lifted = ns["lifted"]

    for bx0 in (1, 2):
        st = CPUState(ax=5, bx=bx0, cx=1, dx=0, sp=0x2000, cs=CS, ip=0x0102,
                      ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)
        asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}), entry=base)
        hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}), entry=base)
        _run_interpreted(asm)
        lifted(hook)
        assert (hook.s.ax, hook.s.bx) == (asm.s.ax, asm.s.bx), f"bx0={bx0}"
        assert (hook.s.cs, hook.s.ip) == (CS, RET_IP)
        assert (hook.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)


def test_indirect_jump_lifts_as_tail_transfer_register_and_table():
    """jmp bx / jmp [table+bx]: the lifted hook must end at the runtime target
    with all other state identical to the interpreter (tail-exit contract)."""
    # 0100: mov bx, 0x0200 ; 0103: jmp bx
    code_reg = bytes.fromhex("BB0002" "FFE3")
    lifted, _scan, _src = _lift(code_reg)
    st = CPUState(ax=1, bx=0, cx=1, dx=0, sp=0x2000, cs=CS, ip=ENTRY,
                  ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)
    asm = _make_cpu(code_reg, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code_reg, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    for _ in range(2):
        asm.step()
    lifted(hook)
    assert (hook.s.cs, hook.s.ip) == (asm.s.cs, asm.s.ip) == (CS, 0x0200)
    assert hook.s.bx == asm.s.bx

    # 0100: mov bx, 2 ; 0103: jmp [0x0010+bx]  (a 2-entry jump table in DS)
    code_tbl = bytes.fromhex("BB0200" "FF67 10".replace(" ", ""))
    table = (0x0111).to_bytes(2, "little") + (0x0222).to_bytes(2, "little")
    lifted2, _scan2, _src2 = _lift(code_tbl)
    st2 = CPUState(ax=0, bx=0, cx=1, dx=0, sp=0x2000, cs=CS, ip=ENTRY,
                   ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)
    asm2 = _make_cpu(code_tbl, CPUState(**{k: getattr(st2, k) for k in st2.__slots__}))
    hook2 = _make_cpu(code_tbl, CPUState(**{k: getattr(st2, k) for k in st2.__slots__}))
    asm2.mem.load(0x4000, 0x0010, table)
    hook2.mem.load(0x4000, 0x0010, table)
    for _ in range(2):
        asm2.step()
    lifted2(hook2)
    assert (hook2.s.cs, hook2.s.ip) == (asm2.s.cs, asm2.s.ip) == (CS, 0x0222)


def test_far_indirect_jump_lifts_as_tail_transfer():
    """jmp far [mem] (ISR chain to the previous vector): sets CS and IP from
    the far pointer and returns."""
    # 0100: jmp far [0x0020]
    code = bytes.fromhex("FF2E2000")
    farptr = (0x0333).to_bytes(2, "little") + (0x5000).to_bytes(2, "little")
    lifted, _scan, _src = _lift(code)
    st = CPUState(ax=0, bx=0, cx=1, dx=0, sp=0x2000, cs=CS, ip=ENTRY,
                  ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    asm.mem.load(0x4000, 0x0020, farptr)
    hook.mem.load(0x4000, 0x0020, farptr)
    asm.step()
    lifted(hook)
    assert (hook.s.cs, hook.s.ip) == (asm.s.cs, asm.s.ip) == (0x5000, 0x0333)


def test_linked_direct_call_replaces_emulate_call_and_stays_exact():
    """The linker seam: a near CALL to a lifted callee emits a direct native
    call (call_installed_hook_like_near_call) instead of emulate_call — no
    interpreter in the call path, byte-exact against the interpreted original.
    This is the de-VM step of the recovery pipeline, proven in miniature."""
    # 0100: mov ax, 2 ; 0103: call 0x0110 ; 0106: add ax, 1 ; 0109: ret
    # 0110: add ax, 5 ; 0113: ret                       (the callee)
    code = bytes.fromhex("B80200" "E80A00" "050100" "C3"
                         "909090909090"                   # padding to 0x0110
                         "050500" "C3")
    fetch = lambda off: code[(off - ENTRY) & 0xFFFF] if 0 <= (off - ENTRY) < len(code) else 0x90

    from dos_re.lift.cfg import scan_function as _scan_fn
    callee_scan = _scan_fn(fetch, 0x0110)
    assert callee_scan.liftable
    assert all(i.kind == "ret" for i in callee_scan.exits)   # near-linkable
    callee_src = emit_function(callee_scan, CS, "lifted_callee",
                               signature=code[0x10:0x14])
    caller_scan = _scan_fn(fetch, ENTRY)
    caller_src = emit_function(caller_scan, CS, "lifted_caller",
                               signature=code[:6],
                               link_map={0x0110: "lifted_callee"})
    assert "emulate_call" not in caller_src.split("def lifted_caller")[1]
    # Separate module namespaces (as on disk); the callee is injected the way
    # the link tool's link_imports would import it.
    ns_callee: dict = {}
    exec(compile(callee_src, "<callee>", "exec"), ns_callee)   # noqa: S102
    ns: dict = {"lifted_callee": ns_callee["lifted_callee"]}
    exec(compile(caller_src, "<caller>", "exec"), ns)          # noqa: S102

    st = CPUState(ax=0, bx=0, cx=1, dx=0, sp=0x2000, cs=CS, ip=ENTRY,
                  ds=0x4000, es=0x4000, ss=0x3000, flags=0x0202)
    asm = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    hook = _make_cpu(code, CPUState(**{k: getattr(st, k) for k in st.__slots__}))
    _run_interpreted(asm)
    ns["lifted_caller"](hook)
    assert (hook.s.cs, hook.s.ip) == (CS, RET_IP)
    assert hook.s.ax == asm.s.ax == 8            # 2 + 5 + 1
    assert hook.s.sp == asm.s.sp
    assert (hook.s.flags & 0x0FD5) == (asm.s.flags & 0x0FD5)
