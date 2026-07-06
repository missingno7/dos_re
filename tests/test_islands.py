"""Smoke tests for the promoted @oracle_link island registry (dos_re/islands.py)."""
from __future__ import annotations

import re
import sys
import textwrap

import pytest

from dos_re.islands import OracleLink, collect_islands, oracle_link, render_manifest

_BOUNDARY = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$")


def test_oracle_link_attaches_metadata_and_returns_function_unchanged():
    @oracle_link("1030:348D", "draws one tile row into the work page",
                 status="VERIFIED", merge_target="frame renderer")
    def draw_tile_row(x: int) -> int:
        return x + 1

    assert draw_tile_row(1) == 2
    link = draw_tile_row.oracle_link
    assert isinstance(link, OracleLink)
    assert _BOUNDARY.match(link.boundary)
    assert link.status == "VERIFIED"


def test_oracle_link_rejects_unknown_status():
    with pytest.raises(ValueError):
        oracle_link("1030:0001", "x", status="PROBABLY_FINE")


def _make_fake_adapter_package(tmp_path):
    pkg = tmp_path / "fake_adapter"
    (pkg / "recovered").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "recovered" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "recovered" / "rules.py").write_text(textwrap.dedent("""
        from dos_re.islands import oracle_link

        @oracle_link("1010:D007", "advances one game tick", status="ASM_MATCHED",
                     merge_target="game loop")
        def tick(state):
            return state

        @oracle_link("1010:26FA", "draws the object list", merge_target="renderer")
        def draw_objects(state):
            return state

        def helper_without_link():
            pass
    """), encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    return "fake_adapter"


def test_collect_and_render_manifest_discovers_decorated_functions(tmp_path):
    pkg_name = _make_fake_adapter_package(tmp_path)
    try:
        islands = collect_islands((f"{pkg_name}.recovered",))
        names = [(name, link.boundary) for _mod, name, link in islands]
        assert names == [("draw_objects", "1010:26FA"), ("tick", "1010:D007")]

        manifest = render_manifest((f"{pkg_name}.recovered",))
        assert "| `1010:D007` | `recovered.rules.tick` | ASM_MATCHED | game loop |" in manifest
        assert "helper_without_link" not in manifest
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith(pkg_name)]:
            del sys.modules[mod]
