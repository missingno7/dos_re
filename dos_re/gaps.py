"""Fail-loud gap exceptions + hybrid-runtime bookkeeping scaffolding.

Two proven patterns from the source ports, promoted:

**1. The gap exception.** When the hybrid or native runtime reaches behaviour
that is not yet recovered, it raises :class:`HybridGap` — loudly, with a
precise message — instead of silently falling back to the original ASM or
guessing. A silent fallback hides missing recovery work; a loud gap *is* the
next work item. (See docs/methodology.md "Fail-fast over guessed fallback".)

**2. Transition signals.** Some per-frame steps legitimately reach multi-frame
TRANSITIONS that must be driven outside the single-frame loop (death/respawn
sequences, level-end loads, game-over restarts, cutscene reveals). The proven
pattern is to define them as *subclasses* of :class:`HybridGap` in the game
adapter and raise them where the transition begins, e.g.::

    class MyGameRespawnTransition(HybridGap):
        '''Signal (not a gap): the gameplay step reached the death-respawn
        transition; the flow driver drives the respawn generator, rendering
        each frame, then resumes the per-frame loop.'''

Subclassing :class:`HybridGap` means every existing ``except HybridGap`` site
still treats an *unhandled* transition as "not a plain per-frame step" (fail
loud), while the flow driver catches the specific signal FIRST and drives the
multi-frame sequence. Running a 60-frame death bounce *inside* the frame step
would render only its end state — hence the signal.

Origin: generalized from the Prehistorik 2 port's ``pre2/gaps.py`` (whose
``Pre2HybridGap`` + six transition signals drove the full native game flow).
This module stays pure — no cpu/mem imports — so a game's native (VM-less)
layer can import it without pulling in the VM.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class HybridGap(RuntimeError):
    """The hybrid/native runtime reached something not yet recovered.

    Raised loudly instead of silently falling back to the original ASM — a
    silent fallback would hide missing recovery work. Subclass it in the game
    adapter for multi-frame transition *signals* (see module docstring).
    """


@dataclass
class HookVerifyStats:
    """Verified/diverged tallies for checkpoint verifiers (see :func:`report`)."""
    verified: int = 0
    diverged: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class HookTraceStats:
    """Per-hook invocation counts for the live hybrid runtime — which recovered systems
    are actually firing (and, by their absence, which screens are still pure ASM). No
    oracle, no diff: just a tally of the real replacement hooks as they run."""
    counts: dict = field(default_factory=dict)

    def bump(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> dict:
        """A copy of the cumulative counts — pass to ``summary``/``window_total`` as ``since``
        to get a *window* (delta) view: only the hooks that fired since that snapshot."""
        return dict(self.counts)

    def window_total(self, since: dict | None) -> int:
        """Total fires since the ``since`` snapshot (cumulative total if ``since`` is None)."""
        if since is None:
            return self.total()
        return sum(max(0, v - since.get(k, 0)) for k, v in self.counts.items())

    def summary(self, group=None, top: int | None = None, since: dict | None = None) -> str:
        """One-line ``name=count`` summary. With ``since`` (a prior :meth:`snapshot`) show only
        the DELTA — the hooks firing in this window — instead of the cumulative totals."""
        src = self.counts
        if since is not None:
            src = {k: v - since.get(k, 0) for k, v in self.counts.items() if v - since.get(k, 0) > 0}
        agg: dict[str, int] = {}
        for name, c in src.items():
            g = group(name) if group else name
            agg[g] = agg.get(g, 0) + c
        items = sorted(agg.items(), key=lambda kv: -kv[1])
        if top is not None:
            items = items[:top]
        empty = "(idle)" if since is not None else "(no recovered hooks fired)"
        return " ".join(f"{n}={c}" for n, c in items) or empty


def report(stats: HookVerifyStats, on_result, raise_on_divergence, name: str, reason):
    """Record one verify outcome: ``reason is None`` means the contract matched.

    Centralises the verified/diverged bookkeeping every subsystem verifier shares,
    so each checkpoint module only computes its own contract diff.
    """
    if reason is None:
        stats.verified += 1
        if on_result is not None:
            on_result(name, True, None)
    else:
        stats.diverged.append((name, reason))
        if on_result is not None:
            on_result(name, False, reason)
        if raise_on_divergence:
            raise AssertionError(f"hook verify divergence on {name}: {reason}")
