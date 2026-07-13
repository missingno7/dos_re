"""CPU386.call_through — delegate a sub-call to the interpreter from a hook."""
from __future__ import annotations

from dos_re.cpu386 import CPU386, FlatMemory, EAX, ESP

FUNC = 0x2000
DATA = 0x3000
STACK = 0x8000

# FUNC(arg0): mov eax,[esp+4] ; add eax,3 ; mov [0x3000],eax ; ret
FUNC_CODE = bytes.fromhex("8B442404" "83C003" "A300300000" "C3")


def _cpu():
    mem = FlatMemory(size=0x10000 * 8)
    mem.load(FUNC, FUNC_CODE)
    return CPU386(mem, eip=0x1000, esp=STACK)


def test_call_through_returns_and_restores_stack():
    cpu = _cpu()
    esp0 = cpu.r[ESP]
    ret = cpu.call_through(FUNC, (10,))
    assert ret == 13                       # eax = arg0 + 3
    assert cpu.mem.r32(DATA) == 13         # the memory side effect happened
    assert cpu.r[ESP] == esp0              # cdecl args cleaned; stack restored


def test_call_through_no_args():
    cpu = _cpu()
    cpu.mem.w32(STACK + 4, 40)             # a value the func will read as arg0
    esp0 = cpu.r[ESP]
    # with no declared args, call_through cleans nothing; the func reads
    # whatever is at [esp+4] after its own return-address push
    cpu.call_through(FUNC, ())
    assert cpu.r[ESP] == esp0


def test_call_through_suppresses_irq_during_call():
    cpu = _cpu()
    fired = []
    cpu.pending_irq = lambda: fired.append(1) or 0   # would fire every poll
    cpu.eflags |= 0x200                                # IF set
    cpu.call_through(FUNC, (1,))
    assert fired == []                                 # suppressed for the call
    assert cpu.pending_irq is not None                 # ...and restored after
