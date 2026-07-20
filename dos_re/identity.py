"""Stable, backend-independent identities for recovered programs.

Original-program identity is intentionally separate from generated module
names, runtime objects, symbols, or a particular execution backend.  Atlas,
replay, planning, and implementation catalogs exchange only these canonical
strings.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote


def _component(value: str, name: str) -> str:
    value = str(value)
    if not value:
        raise ValueError(f"{name} must not be empty")
    return quote(value, safe="-._~")


def _decode(value: str, name: str) -> str:
    decoded = unquote(value)
    if not decoded:
        raise ValueError(f"{name} must not be empty")
    return decoded


def _digest(value: str, algorithm: str) -> str:
    value = str(value).lower()
    algorithm = str(algorithm).lower()
    expected = {"sha1": 40, "sha256": 64}.get(algorithm)
    if expected is None or len(value) != expected:
        raise ValueError(f"invalid {algorithm} content digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"invalid {algorithm} content digest") from exc
    return value


def real_mode_address(segment: int, offset: int) -> str:
    """Return the canonical local address for 16-bit segmented code."""
    if not 0 <= int(segment) <= 0xFFFF or not 0 <= int(offset) <= 0xFFFF:
        raise ValueError("real-mode segment and offset must be 16-bit")
    return f"{int(segment):04x}:{int(offset):04x}"


def flat_address(address: int, *, width: int = 8) -> str:
    """Return a fixed-width canonical address in an explicit flat space."""
    if int(address) < 0 or width <= 0 or int(address) >= 16 ** int(width):
        raise ValueError("flat address does not fit the requested width")
    return f"{int(address):0{int(width)}x}"


@dataclass(frozen=True)
class ProgramIdentity:
    """One recovered program/product.

    ``key`` preserves an existing project identity such as ``skyroads:1.0``.
    It is opaque: only the structured child components are escaped.
    """

    key: str

    def __post_init__(self) -> None:
        if not str(self.key) or str(self.key).startswith(":") or str(self.key).endswith(":"):
            raise ValueError("program key must not be empty or colon-delimited at an edge")
        object.__setattr__(self, "key", str(self.key))

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class ImageIdentity:
    program: ProgramIdentity
    label: str
    hash_algorithm: str
    content_digest: str

    def __post_init__(self) -> None:
        algorithm = str(self.hash_algorithm).lower()
        object.__setattr__(self, "label", str(self.label))
        _component(self.label, "image label")
        object.__setattr__(self, "hash_algorithm", algorithm)
        object.__setattr__(self, "content_digest", _digest(self.content_digest, algorithm))

    @property
    def key(self) -> str:
        return (
            f"{self.program}:image:{_component(self.label, 'image label')}:"
            f"{self.hash_algorithm}:{self.content_digest}"
        )

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class AddressIdentity:
    image: ImageIdentity
    address_space: str
    address: str

    _kind = "point"

    def __post_init__(self) -> None:
        object.__setattr__(self, "address_space", str(self.address_space))
        object.__setattr__(self, "address", str(self.address).lower())
        _component(self.address_space, "address space")
        _component(self.address, "address")

    @property
    def key(self) -> str:
        return (
            f"{self.image}:{self._kind}:"
            f"{_component(self.address_space, 'address space')}:"
            f"{_component(self.address, 'address')}"
        )

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class FunctionIdentity(AddressIdentity):
    _kind = "function"


@dataclass(frozen=True)
class ExecutionPointIdentity(AddressIdentity):
    _kind = "point"


@dataclass(frozen=True)
class RuntimeCodeSlotIdentity(AddressIdentity):
    _kind = "runtime-slot"


@dataclass(frozen=True)
class RuntimeCodeVariantIdentity:
    slot: RuntimeCodeSlotIdentity
    hash_algorithm: str
    content_digest: str

    def __post_init__(self) -> None:
        algorithm = str(self.hash_algorithm).lower()
        object.__setattr__(self, "hash_algorithm", algorithm)
        object.__setattr__(self, "content_digest", _digest(self.content_digest, algorithm))

    @property
    def key(self) -> str:
        return f"{self.slot}:variant:{self.hash_algorithm}:{self.content_digest}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class RegionIdentity:
    program: ProgramIdentity
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", str(self.label))
        _component(self.label, "region label")

    @property
    def key(self) -> str:
        return f"{self.program}:region:{_component(self.label, 'region label')}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class BoundaryIdentity:
    program: ProgramIdentity
    namespace: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", str(self.namespace))
        object.__setattr__(self, "label", str(self.label))
        _component(self.namespace, "boundary namespace")
        _component(self.label, "boundary label")

    @property
    def key(self) -> str:
        return (
            f"{self.program}:boundary:{_component(self.namespace, 'boundary namespace')}:"
            f"{_component(self.label, 'boundary label')}"
        )

    def __str__(self) -> str:
        return self.key


def split_child_identity(key: str, marker: str) -> tuple[str, ...]:
    """Split and unescape the components after a structured identity marker."""
    token = f":{marker}:"
    if token not in key:
        raise ValueError(f"identity is not a {marker!r} identity")
    return tuple(_decode(part, marker) for part in key.split(token, 1)[1].split(":"))
