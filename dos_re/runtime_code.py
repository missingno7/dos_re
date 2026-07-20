"""Runtime-code variant and staticization support.

Some addresses in a DOS executable are not single static routines: the cold
executable can contain one routine body while startup/gameplay materializes a
different body at the same CS:IP. Hooking such addresses by address alone is
unsafe: the hook must first prove which live byte variant is installed.

The policy this module encodes is stricter than merely emulating self-modifying
code: runtime-installed bodies are treated as old-school specialization/dispatch
installation. Every accepted body becomes a named, documented, verified static
source implementation. Unknown byte variants fail fast; they are new
reverse-engineering frontiers, not an excuse to run interpreted ASM silently.

(Generalized from Overkill's ``overkill/runtime_code.py`` — the mechanism was
already game-agnostic; only the per-game slot table was parameterized out.)
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Callable, Iterable, Mapping, TextIO

from .memory import linear

Addr = tuple[int, int]


class UnknownRuntimeCodeVariant(RuntimeError):
    """Raised when a hook reaches a runtime-patched address with unknown bytes."""


class RuntimeCodeStaticizationError(RuntimeError):
    """Raised when runtime-code slots are not ready for source-port lifting."""


@dataclass(frozen=True)
class RuntimeCodeVariant:
    addr: Addr
    name: str
    signature: bytes
    island: str
    status: str
    observed_in: tuple[str, ...] = ()
    notes: str = ""

    @property
    def size(self) -> int:
        return len(self.signature)

    @property
    def sha1(self) -> str:
        return sha1(self.signature).hexdigest()

    @property
    def is_accepted_runtime_body(self) -> bool:
        """Whether this variant may be executed by a staticized hook."""
        return self.status.startswith("hooked") or self.status.startswith("staticized")


@dataclass(frozen=True)
class RuntimeCodeStaticization:
    """How a runtime-installed code body is represented in the source port.

    This records the intended transformation:

        patched bytes -> named variant -> explicit static source logic

    It intentionally does not install or mutate code. It is a manifest entry and
    audit target proving that a runtime-code slot has a flat source-port owner.
    """

    source_module: str
    source_function: str
    dispatch: str
    parameters: tuple[str, ...] = ()
    state_inputs: tuple[str, ...] = ()
    asm_visible_side_effects: tuple[str, ...] = ()
    notes: str = ""

    @property
    def target(self) -> str:
        return f"{self.source_module}.{self.source_function}"


@dataclass(frozen=True)
class RuntimeCodeSlot:
    """A polyvariant executable slot in the original runtime image.

    A slot is the stable source-port concept. Variants are the original byte
    bodies observed in that slot. Staticization describes the source logic that
    replaces accepted runtime-installed variants without preserving
    interpreter-level self-modifying behavior.
    """

    addr: Addr
    name: str
    island: str
    owner: Addr | None
    role: str
    variants: tuple[RuntimeCodeVariant, ...]
    staticization: RuntimeCodeStaticization | None
    installer_status: str
    installer_evidence: tuple[str, ...] = ()
    notes: str = ""

    @property
    def max_signature_size(self) -> int:
        return max(v.size for v in self.variants)

    @property
    def accepted_variants(self) -> tuple[RuntimeCodeVariant, ...]:
        return tuple(v for v in self.variants if v.is_accepted_runtime_body)

    @property
    def is_staticized(self) -> bool:
        return self.staticization is not None and bool(self.accepted_variants)

    @property
    def has_installer_evidence(self) -> bool:
        return self.installer_status.startswith("observed") or self.installer_status.startswith("static")


def live_code_bytes(cpu, addr: Addr, size: int) -> bytes:
    seg, off = addr
    start = linear(seg, off)
    return bytes(cpu.mem.data[start:start + size])


def identify_runtime_code_variant(
    cpu, addr: Addr, slots: Mapping[Addr, RuntimeCodeSlot],
) -> RuntimeCodeVariant:
    """Return the known runtime-code variant currently installed at ``addr``.

    The match is exact for the registered signature length. Unknown bytes are a
    reverse-engineering frontier and therefore fail fast.
    """
    slot = slots.get(addr)
    variants = slot.variants if slot is not None else ()
    if not variants:
        raise UnknownRuntimeCodeVariant(
            f"no runtime-code variants are registered for {addr[0]:04X}:{addr[1]:04X}"
        )
    max_len = max(v.size for v in variants)
    live = live_code_bytes(cpu, addr, max_len)
    for variant in variants:
        if live[:variant.size] == variant.signature:
            return variant
    sample = live[:min(64, len(live))]
    expected = "; ".join(f"{v.name}[{v.size}B]={v.signature[:16].hex(' ')}..." for v in variants)
    raise UnknownRuntimeCodeVariant(
        f"unknown runtime-code variant at {addr[0]:04X}:{addr[1]:04X}; "
        f"live[{len(sample)}B]={sample.hex(' ')}; expected one of: {expected}"
    )


def require_runtime_code_variant(
    cpu, addr: Addr, expected_name: str, slots: Mapping[Addr, RuntimeCodeSlot],
) -> RuntimeCodeVariant:
    """Identify the live variant and require that it is the hook's target body."""
    variant = identify_runtime_code_variant(cpu, addr, slots)
    if variant.name != expected_name:
        live = live_code_bytes(cpu, addr, min(64, variant.size))
        raise UnknownRuntimeCodeVariant(
            f"runtime-code variant {variant.name!r} at {addr[0]:04X}:{addr[1]:04X} "
            f"is known but not valid for hook {expected_name!r}; "
            f"status={variant.status}; live={live.hex(' ')}"
        )
    return variant


def describe_live_runtime_code_state(
    cpu, addr: Addr, slots: Mapping[Addr, RuntimeCodeSlot],
) -> dict[str, object]:
    """Return a diagnostic description of the live bytes at a runtime-code slot."""
    slot = slots.get(addr)
    if slot is None:
        raise UnknownRuntimeCodeVariant(
            f"no runtime-code slot is registered for {addr[0]:04X}:{addr[1]:04X}"
        )
    sample = live_code_bytes(cpu, addr, slot.max_signature_size)
    try:
        variant = identify_runtime_code_variant(cpu, addr, slots)
        variant_name = variant.name
        status = variant.status
    except UnknownRuntimeCodeVariant:
        variant_name = "UNKNOWN"
        status = "unknown-live-bytes"
    return {
        "addr": f"{addr[0]:04X}:{addr[1]:04X}",
        "slot": slot.name,
        "variant": variant_name,
        "status": status,
        "sha1": sha1(sample).hexdigest(),
        "bytes": sample.hex(" "),
    }


def runtime_code_staticization_report(
    slots: Mapping[Addr, RuntimeCodeSlot], *, strict_installers: bool = False,
) -> list[dict[str, object]]:
    """Describe every runtime-code slot and whether it is source-port staticized."""
    report: list[dict[str, object]] = []
    for slot in slots.values():
        staticization = slot.staticization
        missing: list[str] = []
        if not slot.accepted_variants:
            missing.append("accepted runtime variant")
        if staticization is None:
            missing.append("static source target")
        if strict_installers and not slot.has_installer_evidence:
            missing.append("installer provenance")
        report.append({
            "addr": f"{slot.addr[0]:04X}:{slot.addr[1]:04X}",
            "slot": slot.name,
            "island": slot.island,
            "accepted_variants": tuple(v.name for v in slot.accepted_variants),
            "all_variants": tuple(v.name for v in slot.variants),
            "staticized": slot.is_staticized,
            "static_target": staticization.target if staticization else "",
            "dispatch": staticization.dispatch if staticization else "",
            "installer_status": slot.installer_status,
            "missing": tuple(missing),
        })
    return report


def assert_runtime_code_staticization_ready(
    slots: Mapping[Addr, RuntimeCodeSlot], *, strict_installers: bool = False,
) -> None:
    """Fail if any accepted runtime-code slot lacks a static source owner.

    This is the project-level gate for the policy "no self-modifying source".
    The default gate allows installer provenance to remain pending while the
    accepted variant is already staticized; pass ``strict_installers=True`` when
    preparing to declare 100% runtime-code exhaustion.
    """
    bad = [row for row in runtime_code_staticization_report(slots, strict_installers=strict_installers) if row["missing"]]
    if bad:
        lines = ["runtime-code staticization is incomplete:"]
        for row in bad:
            missing = ", ".join(row["missing"])
            lines.append(f"  {row['addr']} {row['slot']}: missing {missing}")
        raise RuntimeCodeStaticizationError("\n".join(lines))


@dataclass(frozen=True)
class RuntimeCodeWriteEvent:
    writer: Addr
    target_phys: int
    size: int
    old: bytes
    new: bytes
    matched_region: str

    def line(self) -> str:
        return (
            f"writer={self.writer[0]:04X}:{self.writer[1]:04X} "
            f"target={self.target_phys:05X} size={self.size} "
            f"region={self.matched_region} old={self.old.hex(' ')} new={self.new.hex(' ')}"
        )


class RuntimeCodeWriteTracer:
    """Optional write tracer for discovering code materialization/installers.

    Install it on a CPU to watch writes that overlap runtime-code addresses. It
    is intentionally opt-in so normal gameplay and tests do not pay for code
    write logging.
    """

    def __init__(
        self,
        cpu,
        regions: Iterable[tuple[Addr, int]],
        *,
        sink: Callable[[RuntimeCodeWriteEvent], None] | TextIO | Path | None = None,
    ):
        self.cpu = cpu
        self.regions = tuple(regions)
        self.events: list[RuntimeCodeWriteEvent] = []
        self._sink = sink

    def install(self) -> "RuntimeCodeWriteTracer":
        self.cpu.mem.write_watchers.append(self._on_memory_write)
        return self

    def uninstall(self) -> None:
        try:
            self.cpu.mem.write_watchers.remove(self._on_memory_write)
        except ValueError:
            pass

    def _on_memory_write(self, phys: int, old: bytes, new: bytes) -> None:
        if old == new:
            return
        end = phys + len(new)
        for (seg, off), size in self.regions:
            start = linear(seg, off)
            region_end = start + size
            if phys < region_end and end > start:
                event = RuntimeCodeWriteEvent(
                    writer=(self.cpu.s.cs & 0xFFFF, self.cpu.s.ip & 0xFFFF),
                    target_phys=phys & 0xFFFFF,
                    size=len(new),
                    old=old,
                    new=new,
                    matched_region=f"{seg:04X}:{off:04X}+{size:04X}",
                )
                self.events.append(event)
                self._emit(event)
                break

    def _emit(self, event: RuntimeCodeWriteEvent) -> None:
        sink = self._sink
        if sink is None:
            return
        line = event.line() + "\n"
        if isinstance(sink, Path):
            with sink.open("a", encoding="utf-8") as f:
                f.write(line)
        elif hasattr(sink, "write"):
            sink.write(line)
        else:
            sink(event)


def default_runtime_code_regions(
    slots: Mapping[Addr, RuntimeCodeSlot], *, context: int = 0x40,
) -> tuple[tuple[Addr, int], ...]:
    """A little context after each known signature, to catch nearby tails or a
    variant body growing beyond the current observed end."""
    return tuple((slot.addr, slot.max_signature_size + context) for slot in slots.values())
