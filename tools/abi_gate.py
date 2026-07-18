"""abi_gate.py -- the machine-checkable M3b (ABI-recovered CPUless) wall.

Reports the acceptance counters from docs/abi_end_state.md over an emitted
ABI core corpus.  Every counter must be ZERO; anything non-zero names the
exact files.  Functions that are NOT cores are reported separately as a
CLASSIFIED EXCEPTION report -- each class owes a generated representation,
not merely a name, so an exception is a finding rather than a silent gap.

This is a STATIC gate over generated text and the census: it complements,
never replaces, the seeded differential (tools/abi_core_verify.py) and the
end-to-end oracle demo.  It exists because several real defects in this
milestone were statically visible before they were dynamically visible --
an unbound composed-call argument, a stale core module left on disk after
its function was refused.

Usage (from the game root):
    python dos_re/tools/abi_gate.py \
        --abi-dir lemmings/recovered_abi \
        --census artifacts/abi/contract_census.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 16-bit register names that must never appear as a PUBLIC parameter or as
#: a result index in a recovered contract.
_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ss", "ds", "es",
         "cs")


def _core_files(abi_dir: Path):
    return sorted(abi_dir.glob("core_*.py"))


def gate(abi_dir: Path, census: dict) -> dict:
    files = _core_files(abi_dir)
    manifest_path = abi_dir / "cores_manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.is_file() else {"cores": []})
    keys = list(manifest.get("cores", ()))

    counters: dict[str, list[str]] = {k: [] for k in (
        "cpu_object_imports",
        "register_named_public_params",
        "public_ss_or_sp_params",
        "register_keyed_results",
        "virtual_stack_objects",
        "historical_stack_memory_access",
        "return_address_writes",
        "unbound_composed_call_args",
        "stale_core_modules",
        "cores_missing_contract_metadata",
    )}

    keep = {f"core_{k.replace(':', '_').lower()}.py" for k in keys}
    for p in files:
        if p.name not in keep:
            counters["stale_core_modules"].append(p.name)

    for p in files:
        s = p.read_text(encoding="utf-8")
        name = p.name
        # a recovered core must not reach the CPU carrier or interpreter
        if re.search(r"^\s*(from|import)\s+dos_re\.(cpu|runtime|lift)", s,
                     re.M):
            counters["cpu_object_imports"].append(name)
        for m in re.finditer(r"^def (?:_abi_core|abi_\w+)\(([^)]*)\)", s,
                             re.M):
            sig = m.group(1)
            for reg in _REGS:
                if re.search(rf"\b{reg}\s*=", sig):
                    counters["register_named_public_params"].append(
                        f"{name}:{reg}")
                    if reg in ("ss", "sp"):
                        counters["public_ss_or_sp_params"].append(
                            f"{name}:{reg}")
        if re.search(r"_o\['[a-z]{2}'\]", s):
            counters["register_keyed_results"].append(name)
        if re.search(r"\b_vs\b", s):
            counters["virtual_stack_objects"].append(name)
        # A de-stacked core must not address memory through the MACHINE
        # STACK.  mem.*(ss, ...) is only a violation when ss is NOT a
        # declared semantic segment parameter: slice 7 (ss-as-data) proved
        # some functions use ss purely as a data-segment selector -- there
        # the access is no more "stack" than a ds: one, and ss arrives as an
        # ordinary contract parameter.  When ss is NOT a parameter, the
        # reference is either the historical stack or plain unbound.
        ss_is_param = bool(re.search(
            r"'role': '\w+', 'historical': 'ss'", s))
        if re.search(r"mem\.[rw][bw]\(ss,", s) and not ss_is_param:
            counters["historical_stack_memory_access"].append(name)
        # every segment register used to address memory must be BOUND in
        # this module (a param or an assignment) -- generalises the
        # composed-call check to memory operands.
        sm = re.search(r"def _abi_core\(([^)]*)\)", s)
        if sm:
            bound = {q.strip().split("=")[0] for q in sm.group(1).split(",")}
            bound |= set(re.findall(r"^\s+([a-z_][a-z0-9_]*) = ", s, re.M))
            for seg in set(re.findall(r"mem\.[rw][bw]\((\w+),", s)):
                if seg not in bound and not seg.isdigit():
                    counters["unbound_composed_call_args"].append(
                        f"{name}:mem-seg {seg}")
        # the mechanical return-address idiom: writing the next ip to ss:sp
        if re.search(r"mem\.ww\(ss,\s*sp", s):
            counters["return_address_writes"].append(name)
        if "_CONTRACT" not in s:
            counters["cores_missing_contract_metadata"].append(name)
        # every composed-call argument must be bound in this module
        sig_m = re.search(r"def _abi_core\(([^)]*)\)", s)
        if sig_m:
            avail = {q.strip().split("=")[0]
                     for q in sig_m.group(1).split(",")}
            avail |= set(re.findall(r"^\s+([a-z_][a-z0-9_]*) = ", s, re.M))
            for call in re.finditer(r"_core_[0-9a-f_]+\(([^)]*)\)", s):
                for a in call.group(1).split(","):
                    a = a.strip()
                    if not a or "=" in a or a in ("mem", "plat"):
                        continue
                    if a not in avail:
                        counters["unbound_composed_call_args"].append(
                            f"{name}:{a}")

    # CLASSIFIED EXCEPTIONS: every function kept mechanical, by the exact
    # capability that blocked it.  The promote tool records this in the
    # manifest; fall back to the census refusal when it is absent.
    core_keys = set(keys)
    exceptions: Counter = Counter()
    refused_map = manifest.get("refused") or {}
    for key, prop in census["functions"].items():
        if key in core_keys:
            continue
        if key in refused_map:
            exceptions[refused_map[key]] += 1
        elif prop["refusals"]:
            exceptions[prop["refusals"][0]["reason"]] += 1
        else:
            exceptions["unclassified (regenerate with abi_promote --cores)"] += 1
    return {"cores": len(keys), "counters": counters,
            "exceptions": dict(exceptions.most_common())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--abi-dir", required=True)
    ap.add_argument("--census", required=True)
    args = ap.parse_args(argv)

    census = json.loads(Path(args.census).read_text(encoding="utf-8"))
    rep = gate(Path(args.abi_dir), census)

    print(f"M3b ABI-recovered wall over {rep['cores']} emitted cores:")
    failed = 0
    for name, hits in rep["counters"].items():
        n = len(hits)
        failed += n
        mark = "ok " if n == 0 else "FAIL"
        print(f"  [{mark}] {name:<38} {n:4d}"
              + ("" if not hits else "  " + ", ".join(sorted(hits)[:4])
                 + (" ..." if n > 4 else "")))
    print("\nclassified exceptions (each owes a generated representation):")
    for reason, n in rep["exceptions"].items():
        print(f"  {reason:<44} {n:4d}")

    if failed:
        print(f"\nWALL NOT CLOSED: {failed} violation(s).")
        return 1
    print("\nWALL COUNTERS ALL ZERO for the emitted core corpus. "
          "(M3b completion additionally requires the exception list above "
          "to be empty or fully represented, plus a green differential and "
          "a green oracle demo.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
