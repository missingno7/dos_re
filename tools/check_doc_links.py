"""Check that every relative markdown link in a doc tree resolves to a real file.

The docs across this ecosystem (framework reference, porting methodology, ledgers) are densely
cross-linked and agent-maintained; a broken relative link silently strands the next agent mid-boot-
sequence. This makes the check mechanical: scan ``*.md``, resolve every relative link against the file's
directory, report the ones that don't exist. External (``http``/``mailto``) links and pure in-page
anchors are ignored; a ``path#anchor`` link is checked for the path only.

Usage:
    python tools/check_doc_links.py [root ...] [--exclude NAME ...]

Defaults: root = the repo containing this tools/ directory; excluded directory names = .git,
__pycache__, .pytest_cache, node_modules. Pass extra ``--exclude`` names to skip vendored/submodule
trees a run doesn't own (e.g. a porting repo runs
``python dos_re/tools/check_doc_links.py . --exclude dos_re`` — the submodule checks itself in its own
CI). Exit code 0 = all links resolve, 1 = broken links (listed), 2 = usage error.

When to use: after any doc edit, in CI on every push, and before claiming a docs task done.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_LINK = re.compile(r"\]\(([^)\s]+?)(#[^)]*)?\)")
_DEFAULT_EXCLUDES = {".git", "__pycache__", ".pytest_cache", "node_modules"}


def iter_md_files(root: Path, excludes: set[str]):
    for p in sorted(root.rglob("*.md")):
        if not any(part in excludes for part in p.relative_to(root).parts):
            yield p


def broken_links(root: Path, excludes: set[str]) -> tuple[int, list[str]]:
    """Return ``(files_checked, ["file: link", ...])`` for every relative link that doesn't resolve."""
    bad: list[str] = []
    n = 0
    for md in iter_md_files(root, excludes):
        n += 1
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in _LINK.finditer(text):
            link = m.group(1)
            if link.startswith(("http://", "https://", "mailto:")) or link.startswith("#"):
                continue
            target = (md.parent / link).resolve()
            if not target.exists():
                bad.append(f"{md.relative_to(root)}: {link}")
    return n, bad


def main(argv: list[str]) -> int:
    excludes = set(_DEFAULT_EXCLUDES)
    roots: list[Path] = []
    it = iter(argv)
    for a in it:
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 2
        if a == "--exclude":
            name = next(it, None)
            if name is None:
                print("--exclude needs a directory name", file=sys.stderr)
                return 2
            excludes.add(name)
        else:
            roots.append(Path(a))
    if not roots:
        roots = [Path(__file__).resolve().parents[1]]
    rc = 0
    for root in roots:
        if not root.is_dir():
            print(f"{root}: not a directory", file=sys.stderr)
            return 2
        n, bad = broken_links(root.resolve(), excludes)
        if bad:
            rc = 1
            print(f"{root}: {len(bad)} broken link(s) across {n} md files:")
            for b in bad:
                print(f"  {b}")
        else:
            print(f"{root}: all relative links resolve ({n} md files)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
