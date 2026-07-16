"""tools/lint_independence.py — the static EXE-independence proof, pinned.

Synthetic import trees in tmp_path: forbidden symbol edges fail, deferred
(function-local) imports do not, --package-dir resolves nested-submodule
layouts, an UNRESOLVABLE required local module fails (a hole in the walk is
not a pass), and a bare ".exe" suffix literal (an audit tool's own
comparison constant) is not a path offender."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("lint_independence",
                                               _TOOLS / "lint_independence.py")
lint = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("lint_independence", lint)
_spec.loader.exec_module(lint)


def _run(tmp_path, root_src: str, extra: dict[str, str] | None = None,
         package_dirs: dict[str, Path] | None = None,
         prefixes=("dos_re", "mygame")) -> int:
    root = tmp_path / "runner.py"
    root.write_text(root_src, encoding="utf-8")
    for rel, src in (extra or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return lint.run_lint([root], tmp_path, set(lint.DEFAULT_FORBIDDEN),
                         tuple(prefixes), package_dirs or {})


def test_clean_graph_passes(tmp_path):
    assert _run(tmp_path, "import json\n") == 0


def test_forbidden_symbol_import_fails(tmp_path):
    assert _run(tmp_path,
                "from dos_re.runtime import create_runtime\n") == 1


def test_function_local_forbidden_import_is_deferred_not_fatal(tmp_path):
    src = ("def resume(p):\n"
           "    from dos_re.snapshot import load_snapshot\n"
           "    return load_snapshot(p)\n")
    assert _run(tmp_path, src) == 0


def test_exe_path_literal_fails_but_bare_suffix_does_not(tmp_path):
    assert _run(tmp_path, "EXE = 'GAME.EXE'\n") == 1
    assert _run(tmp_path, "SUFFIXES = ('.exe', '.com')\n") == 0


def test_package_dir_resolves_nested_layout_and_walks_it(tmp_path):
    # mygame lives OUTSIDE the repo root layout — only --package-dir finds
    # it; its module-level forbidden import must then be seen.
    nested = tmp_path / "vendor" / "mygame"
    assert _run(tmp_path, "import mygame.boot\n",
                extra={"vendor/mygame/__init__.py": "",
                       "vendor/mygame/boot.py":
                           "from dos_re.runtime import create_runtime\n"},
                package_dirs={"mygame": nested}) == 1
    # Same tree, clean nested module: passes.
    assert _run(tmp_path, "import mygame.boot\n",
                extra={"vendor/mygame/boot.py": "import json\n"},
                package_dirs={"mygame": nested}) == 0


def test_unresolvable_required_local_module_is_a_hole_not_a_pass(tmp_path):
    assert _run(tmp_path, "import mygame.boot\n") == 1
