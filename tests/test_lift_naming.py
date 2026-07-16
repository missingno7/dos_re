"""Focused tests for dos_re.lift.naming — the manifest-driven module-naming seam.

A port whose recovery IR carries symbol identity emits SYMBOLIC module names
(``simone_srand1.py`` instead of ``lifted_2f99_0a10.py``) and records the
entry→stem mapping in the emit dir's ``graph_manifest.json``; the link/install
machinery resolves entries through it, and falls back to the historical
address-derived names for anything unmanifested.  Game-free (synthetic)."""
from __future__ import annotations

import json

import pytest

from dos_re.lift.install import (_load_module, install_vmless_graph,
                                 resolve_links)
from dos_re.lift.naming import MANIFEST_NAME, GraphNaming, default_stem


class FakeCPU:
    def __init__(self):
        self.replacement_hooks = {}
        self.hook_names = {}


# --- the mapping itself ---------------------------------------------------------

def test_default_naming_without_manifest():
    naming = GraphNaming()
    assert naming.stem(0x1010, 0x0100) == "lifted_1010_0100"
    assert naming.stem(0x1010, 0x0100) == default_stem(0x1010, 0x0100)


def test_manifest_maps_and_falls_back():
    naming = GraphNaming({"1010:0100": "simone_srand1"})
    assert naming.stem(0x1010, 0x0100) == "simone_srand1"
    assert naming.stem_of("1010:0100") == "simone_srand1"
    assert naming.stem(0x1010, 0x0200) == "lifted_1010_0200"   # unmanifested


def test_manifest_rejects_bad_identifiers_and_duplicate_stems():
    with pytest.raises(ValueError, match="identifier"):
        GraphNaming({"1010:0100": "not a name"})
    with pytest.raises(ValueError, match="unique"):
        GraphNaming({"1010:0100": "twin", "1010:0200": "twin"})


def test_save_load_round_trip_is_deterministic(tmp_path):
    naming = GraphNaming({"1010:0200": "b_fn", "1010:0100": "a_fn"})
    p1 = naming.save(tmp_path).read_text()
    p2 = naming.save(tmp_path).read_text()
    assert p1 == p2                                            # byte-identical
    loaded = GraphNaming.load(tmp_path)
    assert loaded.mapping == {"1010:0100": "a_fn", "1010:0200": "b_fn"}
    assert GraphNaming.load(tmp_path / "nowhere").mapping == {}


def test_load_rejects_unknown_manifest_version(tmp_path):
    (tmp_path / MANIFEST_NAME).write_text(json.dumps({"version": 99}))
    with pytest.raises(ValueError, match="version"):
        GraphNaming.load(tmp_path)


# --- resolve_links through the manifest ------------------------------------------

def test_resolve_links_binds_through_symbolic_names(tmp_path):
    (tmp_path / "game_callee.py").write_text(
        "def game_callee(cpu):\n    cpu.s.ax = 0x77\n")
    (tmp_path / "game_caller.py").write_text(
        'LINKS = {"1010:0200": None}\n'
        "def game_caller(cpu):\n    LINKS['1010:0200'](cpu)\n")
    GraphNaming({"1010:0100": "game_caller",
                 "1010:0200": "game_callee"}).save(tmp_path)

    loaded = {"game_caller": _load_module(tmp_path / "game_caller.py")}
    n = resolve_links(loaded, tmp_path)                        # naming from disk
    assert n == 1
    assert loaded["game_caller"].LINKS["1010:0200"].__name__ == "game_callee"

    cpu = FakeCPU()

    class S:
        ax = 0
    cpu.s = S()
    loaded["game_caller"].game_caller(cpu)
    assert cpu.s.ax == 0x77


def test_resolve_links_missing_symbolic_callee_fails_loud(tmp_path):
    (tmp_path / "game_caller.py").write_text(
        'LINKS = {"1010:0200": None}\n'
        "def game_caller(cpu):\n    pass\n")
    GraphNaming({"1010:0200": "game_callee"}).save(tmp_path)   # module not on disk
    loaded = {"game_caller": _load_module(tmp_path / "game_caller.py")}
    with pytest.raises(FileNotFoundError, match="game_callee"):
        resolve_links(loaded, tmp_path)


# --- install_vmless_graph through the manifest ------------------------------------

def test_install_vmless_graph_uses_manifest_names(tmp_path):
    (tmp_path / "game_fn.py").write_text("def game_fn(cpu):\n    pass\n")
    (tmp_path / "lifted_1010_0300.py").write_text(
        "def lifted_1010_0300(cpu):\n    pass\n")
    GraphNaming({"1010:0100": "game_fn"}).save(tmp_path)

    cpu = FakeCPU()
    installed = install_vmless_graph(cpu, tmp_path)
    assert installed == {(0x1010, 0x0100): "game_fn.py",       # manifested
                         (0x1010, 0x0300): "lifted_1010_0300.py"}  # default-named
    assert cpu.hook_names[(0x1010, 0x0100)] == "game_fn"
    assert cpu.hook_names[(0x1010, 0x0300)] == "lifted_1010_0300"


def test_install_vmless_graph_missing_manifested_module_fails_loud(tmp_path):
    GraphNaming({"1010:0100": "game_fn"}).save(tmp_path)       # no game_fn.py
    with pytest.raises(FileNotFoundError, match="game_fn"):
        install_vmless_graph(FakeCPU(), tmp_path)
