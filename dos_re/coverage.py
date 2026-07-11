"""Coverage telemetry — the measured "native %" progress metric, as a framework collector.

The CPU emits generic events when ``cpu.coverage_telemetry`` is set: one call per interpreted
instruction and one per hook dispatch (verified / unverified / skipped — the verifier reports the
measured ASM-equivalent instruction count for verified calls). This module is the collector those
events feed: it classifies addresses into adapter-named islands, accumulates the counts, and renders
the honest headline number every port reports:

    native % = hook-covered ASM-equivalent instructions
               ─────────────────────────────────────────────────────────────
               interpreted + bounded-original + hook-covered ASM-equivalent

Honesty rules, baked in (learned on the source ports):

* **Hooked work is measured in ASM-equivalent instructions**, not hook calls — one dispatch can
  replace a 3-instruction leaf or a 10,000-instruction decoder, so counting calls flatters nothing
  and measures nothing. The verifier's oracle run measures it exactly for verified calls; unverified
  calls use the per-hook average from a previous verified run (the JSON cache) and are reported as
  *estimated*; calls with no measurement at all are reported as **unmeasured**, outside the
  percentage — never guessed into it.
* **Oracle-side execution is not game progress.** Wrap reference/oracle runs in
  :meth:`CoverageCollector.bounded_original` so their interpreted instructions count as
  ``bounded_original`` (measured denominator, clearly not "remaining ASM to recover").
* **The classifier is the adapter's** — a plain callable ``(addr, name="") -> island_name``. The
  framework invents no taxonomy; unclassified addresses land in ``"unknown"`` and the report shows
  them loudly (they are the triage frontier, see ``frontier.py``).

Wire-up (porting guide step 8, now a few lines):

    from dos_re.coverage import CoverageCollector
    cov = CoverageCollector(classifier=my_classifier, cache_path=Path("artifacts/coverage_cache.json"))
    rt.cpu.coverage_telemetry = cov
    ... run a demo replay ...
    print(cov.format_summary())

Worked example of a full game-side build-out (regions, categories, a live dashboard):
``overkill_port``'s ``overkill/coverage.py`` — this module is its generic core, promoted.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

Addr = tuple[int, int]

__all__ = ["Addr", "CoverageCollector", "HookStats", "IslandStats", "fmt_addr"]


def fmt_addr(addr: Addr) -> str:
    return f"{addr[0]:04X}:{addr[1]:04X}"


@dataclass
class HookStats:
    """Per-hook accumulation. ``asm_equiv`` is the verifier-measured replaced-instruction total over
    the verified calls; ``estimated_equiv`` accumulates cache-average estimates for unverified calls."""
    addr: Addr
    name: str
    island: str
    calls: int = 0
    verified_calls: int = 0
    unverified_calls: int = 0
    skipped: int = 0
    asm_equiv: int = 0                 # measured (verified calls)
    estimated_equiv: float = 0.0       # cache-estimated (unverified calls with a known average)
    unmeasured_calls: int = 0          # unverified calls with NO estimate — outside the percentage
    last_asm_equiv: int = 0

    @property
    def total_equiv(self) -> float:
        return self.asm_equiv + self.estimated_equiv


@dataclass
class IslandStats:
    """Per-island accumulation (islands are created lazily from the classifier's return values)."""
    interpreted: int = 0               # interpreted ASM instructions still running in this island
    bounded: int = 0                   # interpreted under bounded_original (oracle-side, not "remaining")
    hook_calls: int = 0
    hook_equiv: float = 0.0            # measured + estimated ASM-equivalent instructions


class CoverageCollector:
    """Thread-safe collector for ``cpu.coverage_telemetry``.

    ``classifier(addr, name="") -> str`` maps an address (and, for hooks, the hook name) to an island;
    omit it and everything lands in ``"unknown"``. ``cache_path`` (optional JSON) persists per-hook
    average ASM-equivalents across runs, so a replay without the verifier still *estimates* hooked
    work instead of dropping it into "unmeasured"."""

    def __init__(self, *, classifier: Callable[..., str] | None = None,
                 cache_path: Path | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self.classifier = classifier or (lambda addr, name="": "unknown")
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._lock = threading.RLock()
        self.interpreted_hits: Counter[Addr] = Counter()
        self.hooks: dict[Addr, HookStats] = {}
        self.islands: dict[str, IslandStats] = {}
        self.total_interpreted = 0
        self.total_bounded = 0
        self.total_hook_calls = 0
        self._bounded_depth = 0
        self._cache: dict[str, dict[str, float]] = {}
        self._load_cache()

    # -- classification ------------------------------------------------------------------------------
    def _classify(self, addr: Addr, name: str = "") -> str:
        try:
            island = self.classifier(addr, name)
        except TypeError:                       # a 1-arg classifier is fine too
            island = self.classifier(addr)
        return island or "unknown"

    def _island(self, name: str) -> IslandStats:
        st = self.islands.get(name)
        if st is None:
            st = self.islands[name] = IslandStats()
        return st

    def _hook(self, addr: Addr, name: str) -> HookStats:
        st = self.hooks.get(addr)
        if st is None:
            st = self.hooks[addr] = HookStats(addr=addr, name=name, island=self._classify(addr, name))
        elif name and st.name != name:
            st.name = name
        return st

    # -- the CPU/verifier hook points ------------------------------------------------------------------
    def record_interpreted_instruction(self, addr: Addr) -> None:
        if not self.enabled:
            return
        with self._lock:
            island = self._island(self._classify(addr))
            if self._bounded_depth:
                self.total_bounded += 1
                island.bounded += 1
            else:
                self.interpreted_hits[addr] += 1
                self.total_interpreted += 1
                island.interpreted += 1

    def record_hook_verified(self, addr: Addr, name: str, asm_equiv_instructions: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            st = self._hook(addr, name)
            st.calls += 1
            st.verified_calls += 1
            st.asm_equiv += int(asm_equiv_instructions)
            st.last_asm_equiv = int(asm_equiv_instructions)
            self.total_hook_calls += 1
            isl = self._island(st.island)
            isl.hook_calls += 1
            isl.hook_equiv += int(asm_equiv_instructions)

    def record_hook_unverified(self, addr: Addr, name: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            st = self._hook(addr, name)
            st.calls += 1
            st.unverified_calls += 1
            self.total_hook_calls += 1
            isl = self._island(st.island)
            isl.hook_calls += 1
            avg = self._cached_avg(addr, st)
            if avg is not None:
                st.estimated_equiv += avg
                isl.hook_equiv += avg
            else:
                st.unmeasured_calls += 1

    def record_hook_skipped(self, addr: Addr, name: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            st = self._hook(addr, name)
            st.skipped += 1

    @contextmanager
    def bounded_original(self):
        """Mark a span whose interpreted instructions are ORACLE-side (verifier reference runs): they
        count as ``bounded_original`` — measured, but never 'remaining ASM to recover'."""
        with self._lock:
            self._bounded_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._bounded_depth = max(0, self._bounded_depth - 1)

    # -- the per-hook average cache ---------------------------------------------------------------------
    def _cached_avg(self, addr: Addr, st: HookStats) -> float | None:
        if st.verified_calls:                                  # this run's own measurement beats the cache
            return st.asm_equiv / st.verified_calls
        rec = self._cache.get(fmt_addr(addr))
        return float(rec["avg_asm_equiv"]) if rec and "avg_asm_equiv" in rec else None

    def _load_cache(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            hooks = raw.get("hooks") if isinstance(raw, dict) else None
            self._cache = hooks if isinstance(hooks, dict) else {}
        except Exception:                                       # noqa: BLE001 — a stale cache is dropped, not fatal
            self._cache = {}

    def save_cache(self) -> None:
        """Persist per-hook average ASM-equivalents (verified calls only) for later estimate-mode runs."""
        if self.cache_path is None:
            return
        with self._lock:
            hooks = dict(self._cache)
            for addr, st in self.hooks.items():
                if st.verified_calls > 0:
                    hooks[fmt_addr(addr)] = {"avg_asm_equiv": st.asm_equiv / st.verified_calls,
                                             "samples": float(st.verified_calls), "name": st.name,
                                             "island": st.island}
            payload = {"version": 1, "hooks": hooks}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    # -- reporting ---------------------------------------------------------------------------------------
    def native_percent(self) -> float:
        """The headline: hook-covered ASM-equivalent work over all measured work (module docstring)."""
        with self._lock:
            hook_equiv = sum(st.total_equiv for st in self.hooks.values())
            measured = self.total_interpreted + self.total_bounded + hook_equiv
            return 100.0 * hook_equiv / measured if measured else 0.0

    def snapshot(self, *, top_n: int = 12) -> dict[str, Any]:
        with self._lock:
            hook_equiv = sum(st.total_equiv for st in self.hooks.values())
            unmeasured = sum(st.unmeasured_calls for st in self.hooks.values())
            return {
                "native_percent": self.native_percent(),
                "interpreted": self.total_interpreted,
                "bounded_original": self.total_bounded,
                "hook_equiv": hook_equiv,
                "hook_equiv_measured": sum(st.asm_equiv for st in self.hooks.values()),
                "hook_equiv_estimated": sum(st.estimated_equiv for st in self.hooks.values()),
                "unmeasured_hook_calls": unmeasured,
                "total_hook_calls": self.total_hook_calls,
                "islands": {k: vars(v).copy() for k, v in sorted(self.islands.items())},
                "top_hooks": [{"addr": fmt_addr(st.addr), "name": st.name, "island": st.island,
                               "calls": st.calls, "total_equiv": st.total_equiv}
                              for st in sorted(self.hooks.values(), key=lambda s: s.total_equiv,
                                               reverse=True)[:top_n]],
                "top_interpreted": [{"addr": fmt_addr(a), "hits": n, "island": self._classify(a)}
                                    for a, n in self.interpreted_hits.most_common(top_n)],
            }

    def format_summary(self, *, top_n: int = 10) -> str:
        s = self.snapshot(top_n=top_n)
        lines = [
            "coverage summary",
            f"  native %            : {s['native_percent']:6.2f}  (hook ASM-equiv / all measured work)",
            f"  interpreted ASM     : {s['interpreted']:,} instructions (the remaining recovery frontier)",
            f"  bounded original    : {s['bounded_original']:,} (oracle-side reference runs)",
            f"  hook ASM-equivalent : {s['hook_equiv']:,.0f}  (measured {s['hook_equiv_measured']:,}"
            f" + estimated {s['hook_equiv_estimated']:,.0f})",
            f"  hook calls          : {s['total_hook_calls']:,}"
            + (f"  [{s['unmeasured_hook_calls']:,} UNMEASURED — outside the %]"
               if s["unmeasured_hook_calls"] else ""),
            "  islands (interpreted | hook-equiv):",
        ]
        for name, isl in s["islands"].items():
            lines.append(f"    {name:<20} {isl['interpreted']:>12,} | {isl['hook_equiv']:>12,.0f}")
        if s["top_hooks"]:
            lines.append("  top hooks by ASM-equivalent:")
            for h in s["top_hooks"]:
                lines.append(f"    {h['addr']}  {h['total_equiv']:>12,.0f}  {h['island']:<16} {h['name']}")
        if s["top_interpreted"]:
            lines.append("  top remaining interpreted addresses:")
            for t in s["top_interpreted"]:
                lines.append(f"    {t['addr']}  {t['hits']:>12,}  {t['island']}")
        return "\n".join(lines)
