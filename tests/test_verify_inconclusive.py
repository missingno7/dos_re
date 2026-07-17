"""A verifier TIMEOUT is not a divergence.

The strict verifier proves a lifted hook by re-running the ORIGINAL ASM from
the same pre-state to the hook's continuation and diffing machine state.  If
that oracle run exhausts its budget, the verifier learned nothing about the
candidate -- only about its own timeout.  Reporting it as DIVERGED claims the
recovered code is WRONG on evidence that says no such thing, and sends the
reader hunting a bug that does not exist (it did: a long, file-reading
decompressor whose oracle re-run could not finish looked exactly like broken
recovery).

So timeouts raise HookVerifyInconclusive -- a SUBCLASS of HookVerifyDivergence,
because an unverified hook must still stop the run and must never be
promotable, but a distinct type so reporters can name it honestly.
"""
from __future__ import annotations

from dos_re.verification import HookVerifyDivergence, HookVerifyInconclusive


def test_inconclusive_is_a_divergence_subclass() -> None:
    # Subclass: every existing `except HookVerifyDivergence` still stops the
    # run, so an unproven hook can never be silently treated as passing.
    assert issubclass(HookVerifyInconclusive, HookVerifyDivergence)
    exc = HookVerifyInconclusive("HOOK VERIFY ASM TIMEOUT hook=1010:66E6")
    assert isinstance(exc, HookVerifyDivergence)


def test_a_real_mismatch_is_not_inconclusive() -> None:
    # The distinction has to run the other way too, or the new type is useless:
    # a genuine state mismatch must NOT be reportable as "just a timeout".
    exc = HookVerifyDivergence("AX: asm=0001 hook=0002")
    assert not isinstance(exc, HookVerifyInconclusive)


def test_timeouts_raise_the_inconclusive_type() -> None:
    """Both oracle-budget exits in verification.py must use the new type."""
    import inspect

    from dos_re import verification

    src = inspect.getsource(verification)
    # the wall-clock timeout and the step-budget timeout
    assert src.count("raise HookVerifyInconclusive") == 2
    # ...and neither timeout message is raised as a plain divergence any more
    for marker in ("HOOK VERIFY ASM WALL TIMEOUT", "HOOK VERIFY ASM TIMEOUT"):
        i = src.index(marker)
        raise_line = src.rfind("raise ", 0, i)
        assert src[raise_line:i].strip().startswith("raise HookVerifyInconclusive"), marker
