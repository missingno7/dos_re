"""abi_core_verify's PARALLEL path, over a tiny synthetic corpus.

The previous defect lived exactly here: `ex._processes` read after
`shutdown()` (which sets it to None) raised AttributeError inside a `finally`,
breaking every parallel run -- and it shipped because --jobs was never once
executed.  Testing diff_one alone cannot catch that; the bug is in the tool's
process handling, not in the comparison.

So this builds a two-core corpus on disk and runs main() for real, both ways.
The verdicts must agree: parallelism is an execution strategy, never a
difference in what is proven.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

#: a core whose RUNTIME BEHAVIOUR depends on _ITER_CAP: it spins to the cap,
#: so a test can prove the cap actually reached the worker instead of
#: inspecting source text for the forwarding.
_CAP_CORE = '''\
_ITER_CAP = 20000000
_CONTRACT = {'key': '%(key)s', 'params': (), 'returns': ()}


def _abi_core(mem, *, _base=0):
    n = 0
    while n < _ITER_CAP:
        n += 1
    mem.ww(0x1000, 0x10, n & 0xFFFF)
    return (), {'flags': 0, 'fmask': 0, 'cost': 3}


def func_%(stem)s(mem, *, _base=0):
    o, c = _abi_core(mem, _base=_base)
    return {}, c
'''

#: a core and its mechanical twin, both trivial and identical in behaviour
_CORE = '''\
_ITER_CAP = 20000000
_CONTRACT = {'key': '%(key)s', 'params': (), 'returns': ()}


def _abi_core(mem, *, _base=0):
    mem.ww(0x1000, 0x10, 0x%(val)04X)
    return (), {'flags': 0, 'fmask': 0, 'cost': 3}


def func_%(stem)s(mem, *, _base=0):
    o, c = _abi_core(mem, _base=_base)
    return {}, c
'''


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    """A minimal abi-dir + census + IR the tool can actually consume."""
    abi = tmp_path / "abi"
    abi.mkdir()
    (abi / "__init__.py").write_text("")
    keys = ["1010:0100", "1010:0200"]
    for n, k in enumerate(keys):
        stem = k.replace(":", "_").lower()
        (abi / f"core_{stem}.py").write_text(
            _CORE % {"key": k, "stem": stem, "val": 0xAA + n})
    (abi / "cores_manifest.json").write_text(json.dumps({
        "cores": keys, "refused": {}, "not_integrated": {}}))
    census = {"functions": {k: {"params": [], "returns": [],
                                "refusals": []} for k in keys}}
    cpath = tmp_path / "census.json"
    cpath.write_text(json.dumps(census))
    ir = {"functions": {k: {"signature": k} for k in keys}}
    ipath = tmp_path / "ir.json"
    ipath.write_text(json.dumps(ir))
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"abi": abi, "census": cpath, "ir": ipath}


def _argv(c, *extra):
    return ["--ir", str(c["ir"]), "--abi-dir", str(c["abi"]),
            "--abi-base", "abi", "--census", str(c["census"]),
            "--states", "4", *extra]


def _run(c, *extra):
    import abi_core_verify as v
    return v.main(_argv(c, *extra))


def test_parallel_run_completes_without_crashing(corpus):
    """THE regression: shutdown() sets _processes to None, so reading it
    afterwards raised AttributeError -- inside a `finally`, so it fired on
    EVERY parallel run whatever the verdict.  main() returning an int at all
    is the property that was broken.

    The synthetic IR is not liftable, so the workers report a verifier error
    and the verdict is a mismatch; that is fine and deliberate -- this test is
    about the tool's process handling, not about the comparison.
    """
    rc = _run(corpus, "--jobs", "2")
    assert isinstance(rc, int)


def test_scheduling_does_not_change_the_verdict(corpus):
    """Parallelism is an execution strategy, never a difference in what is
    proven: the same corpus must reach the same conclusion either way."""
    seq = _run(corpus)
    par = _run(corpus, "--jobs", "2")
    assert seq == par


def test_parallel_leaves_no_surviving_workers(corpus):
    """A budget breach terminates workers; ordinary completion joins them.
    Either way none may outlive the run -- two did survive a kill earlier."""
    import multiprocessing as mp
    before = len(mp.active_children())
    _run(corpus, "--jobs", "2")
    assert len(mp.active_children()) <= before, "workers survived the run"


def test_a_worker_error_is_reported_not_silently_dropped(corpus, capsys):
    """A worker that raises must become a REPORTED failure: a missing core
    would otherwise be indistinguishable from a passing one."""
    rc = _run(corpus, "--jobs", "2")
    out = capsys.readouterr().out
    assert rc != 0
    # a verifier crash is INTERNAL_ERROR (exit 3), not a divergence: the
    # tooling failed, which proves nothing about the core either way
    assert rc == 3, f"expected internal-error exit, got {rc}"
    assert "VERIFIER ERRORS" in out and "verifier raised" in out


def test_iter_cap_is_forwarded_to_the_pool(corpus):
    """--iter-cap was applied only on the sequential path, so
    `--jobs N --iter-cap X` silently ran at the emitted 20M cap: the flag
    appeared to work while doing nothing."""
    import inspect

    import abi_core_verify as v
    src = inspect.getsource(v.main)
    assert "args.iter_cap" in src.split("initargs=")[1][:120],         "main() must forward iter_cap to the pool initializer"
    assert "iter_cap" in inspect.signature(v._pool_init).parameters


def test_a_typoed_only_selection_is_refused(corpus, capsys):
    """An --only that matches nothing used to verify nothing and exit 0
    announcing every core identical -- worst during bisection, which is
    exactly when --only gets typed by hand."""
    rc = _run(corpus, "--only", "1010:DEAD")
    out = capsys.readouterr().out
    assert rc == 3
    assert "REFUSING" in out


def test_an_empty_manifest_is_refused(tmp_path, corpus, capsys):
    """Nothing compared is not proof."""
    import json
    (corpus["abi"] / "cores_manifest.json").write_text(
        json.dumps({"cores": [], "refused": {}}))
    rc = _run(corpus)
    out = capsys.readouterr().out
    assert rc == 3
    assert "proves nothing" in out


def _spin_corpus(tmp_path, name, keys):
    """A corpus of cores that spin to _ITER_CAP -- runtime, not source text."""
    abi = tmp_path / name
    abi.mkdir()
    (abi / "__init__.py").write_text("")
    for k in keys:
        stem = k.replace(":", "_").lower()
        (abi / f"core_{stem}.py").write_text(
            _CAP_CORE % {"key": k, "stem": stem})
    (abi / "cores_manifest.json").write_text(
        json.dumps({"cores": list(keys), "refused": {}}))
    (tmp_path / f"{name}_c.json").write_text(json.dumps(
        {"functions": {k: {"params": [], "returns": [], "refusals": []}
                       for k in keys}}))
    (tmp_path / f"{name}_i.json").write_text(json.dumps(
        {"functions": {k: {"signature": k} for k in keys}}))
    return abi, tmp_path / f"{name}_c.json", tmp_path / f"{name}_i.json"


def test_iter_cap_is_actually_applied_to_the_loaded_modules(
        tmp_path, monkeypatch):
    """BEHAVIOURAL: the cap must reach the modules _verify_one loads.

    Driving this through the pool would be vacuous here -- the synthetic IR is
    not liftable, so the mechanical reference fails before any core runs, and
    a wall-clock assertion would pass because NOTHING executed.  (It did: the
    first version of this test measured an elapsed time of a run that never
    called the core.)  So the stub goes in at _fresh_mechanical and the cap
    application is exercised directly, in-process.

    What this does NOT cover, honestly: cross-process delivery of the cap, and
    --budget-s termination.  Both need a genuinely liftable IR fixture.
    """
    import abi_core_verify as v
    abi, c, i = _spin_corpus(tmp_path, "capabi", ["1010:0300"])
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = __import__("capabi.core_1010_0300", fromlist=["_abi_core"])
    assert mod._ITER_CAP == 20000000, "fixture should start at the high cap"

    monkeypatch.setattr(v, "_fresh_mechanical",
                        lambda ir, key, cache: (mod.func_1010_0300, None))
    v._pool_init(json.loads(i.read_text()), json.loads(c.read_text()),
                 "capabi", 2, iter_cap=1000)
    key, rep, _dt = v._verify_one("1010:0300")
    assert mod._ITER_CAP == 1000,         "the cap never reached the module the core actually runs in"
    assert rep.verdict is not None
