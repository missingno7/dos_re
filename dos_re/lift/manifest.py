"""The lifter's proof ledger — a per-function status file, JSON-backed.

Kept DELIBERATELY separate from ``dos_re.islands`` / ``oracle_link``: those
track *recovered* source (understood, refactored, part of a subsystem), and
the metrics-honesty rule (docs/lifting_design.md §7) says a mechanically
lifted function is NOT recovered — it is coverage of the *verification*
frontier, not the *understanding* frontier. Mixing the two vocabularies would
let a wall of unread-but-verified Python inflate the "recovered %" that the
campaigns are steered by. So the lifter counts its own tier here.

Status ladder:
    LIFTED          generated; never executed as a replacement
    ORACLE_PASSING  in-situ verified: N calls, 0 divergence, M/K blocks covered
    NOT_REACHED     lifted, installed for a verify run, but never executed
    DIVERGED        a verify run found a byte-level difference vs the ASM oracle
    INSTALLED       running as the default replacement (still SMC-guarded)
    REFACTORED      a human/AI rewrote it into real recovered Python; then it
                    graduates to an oracle_link island and leaves this ledger

The ledger is plain data (no imports beyond the stdlib) so it round-trips
cleanly and diffs readably in git.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATUSES = ("LIFTED", "ORACLE_PASSING", "NOT_REACHED", "DIVERGED",
            "INSTALLED", "REFACTORED")


@dataclass
class LiftRecord:
    entry: str                       # "1010:4537"
    module: str                      # generated file name
    status: str = "LIFTED"
    instructions: int = 0
    blocks: int = 0
    native_pct: float = 0.0          # share emitted without an interpreter fallback
    calls: int = 0                   # times the hook fired during the last verify run
    verified: int = 0                # times it passed the differential oracle
    divergences: int = 0
    blocks_covered: int = 0          # distinct basic blocks a verify run exercised
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"lift status {self.status!r} not in {STATUSES}")

    @property
    def fully_covered(self) -> bool:
        return self.blocks > 0 and self.blocks_covered >= self.blocks


@dataclass
class LiftManifest:
    records: dict[str, LiftRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "LiftManifest":
        p = Path(path)
        if not p.is_file():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls({e: LiftRecord(**r) for e, r in raw.items()})

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {e: asdict(r) for e, r in sorted(self.records.items())}
        p.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")

    def put(self, rec: LiftRecord) -> None:
        self.records[rec.entry] = rec

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self.records.values():
            out[rec.status] = out.get(rec.status, 0) + 1
        return out
