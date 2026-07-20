"""Module-naming policy for emitted generated graphs.

By default every emitted module is named ``lifted_{cs:04x}_{ip:04x}.py``
with a same-named function inside, and graph tooling derives
file names from entry addresses by that pattern.  A port whose recovery IR
carries symbol identity wants SYMBOLIC names (modules named for the original
routines) without forking that machinery — so the mapping becomes data: a
small manifest, ``graph_manifest.json``, living beside the emitted modules:

    {"version": 1,
     "entries": {"CS:IP": "module_stem", ...}}

Every consumer that maps an entry address to its module file
(``install.resolve_links``, ``install.activate_generated_graph``, and
``tools/liftlink.py``) loads ``GraphNaming`` from the emit dir: manifest
entries win, everything else uses the address-derived default stem. The stem
doubles as the
module's function name (the loaders' ``getattr(module, stem)`` convention),
so a stem must be a valid Python identifier.

The POLICY that chooses symbolic stems (symbol source, sanitization,
collision suffixes) belongs to the consuming port, which owns its symbol
table; this module only records, validates, and serves the chosen mapping.
The execution catalog and planner decide whether the resulting graph is
selected; this module only owns names inside the generated artifact.
"""
from __future__ import annotations

import json
from pathlib import Path

#: The manifest file name, looked up inside the emit directory.
MANIFEST_NAME = "graph_manifest.json"


def default_stem(cs: int, ip: int) -> str:
    """The default address-derived module/function stem."""
    return f"lifted_{cs:04x}_{ip:04x}"


def parse_entry(entry: str) -> tuple[int, int]:
    cs, ip = entry.split(":", 1)
    return int(cs, 16), int(ip, 16)


class GraphNaming:
    """Entry-address → module-stem mapping for one emitted graph directory.

    ``mapping`` keys are ``"CS:IP"`` strings (paragraph-base stable
    address keys); values are module stems (file name without ``.py``,
    also the function name inside).  Entries absent from the mapping resolve
    to :func:`default_stem`.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping: dict[str, str] = {}
        seen: dict[str, str] = {}
        for entry, stem in (mapping or {}).items():
            entry = entry.upper()
            if not str(stem).isidentifier():
                raise ValueError(
                    f"graph manifest: stem {stem!r} for {entry} is not a "
                    f"valid Python identifier")
            prev = seen.get(stem)
            if prev is not None:
                raise ValueError(
                    f"graph manifest: stem {stem!r} claimed by both {prev} "
                    f"and {entry} -- stems must be unique")
            seen[stem] = entry
            self.mapping[entry] = stem

    # -- resolution ---------------------------------------------------------
    def stem(self, cs: int, ip: int) -> str:
        return (self.mapping.get(f"{cs & 0xFFFF:04X}:{ip & 0xFFFF:04X}")
                or default_stem(cs & 0xFFFF, ip & 0xFFFF))

    def stem_of(self, entry: str) -> str:
        return self.stem(*parse_entry(entry))

    def module_path(self, emit_dir, cs: int, ip: int) -> Path:
        return Path(emit_dir) / f"{self.stem(cs, ip)}.py"

    def entries(self) -> list[tuple[int, int, str]]:
        """Sorted ``(cs, ip, stem)`` for every MANIFESTED entry."""
        out = []
        for entry, stem in sorted(self.mapping.items()):
            cs, ip = parse_entry(entry)
            out.append((cs, ip, stem))
        return out

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(cls, emit_dir) -> "GraphNaming":
        """The naming in force for ``emit_dir`` (empty = default naming)."""
        path = Path(emit_dir) / MANIFEST_NAME
        if not path.is_file():
            return cls()
        doc = json.loads(path.read_text(encoding="utf-8"))
        version = doc.get("version")
        if version != 1:
            raise ValueError(f"{path}: unsupported graph-manifest version "
                             f"{version!r}")
        return cls(doc.get("entries", {}))

    def save(self, emit_dir) -> Path:
        """Write the manifest deterministically (sorted keys, no timestamps)."""
        path = Path(emit_dir) / MANIFEST_NAME
        path.write_text(json.dumps(
            {"version": 1, "entries": dict(sorted(self.mapping.items()))},
            indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return path
