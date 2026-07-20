"""Compact, backend-neutral observable-effect accumulation for replay.

The replay verifier needs an order-sensitive account of effects that can
escape a semantic boundary.  It must not build a Python object graph in the
hot path.  :class:`RollingEffectDigest` therefore owns one fixed packing
buffer and feeds primitive integer records directly into SHA-256.

Backends emit the same canonical effect identifiers even when their internal
implementations differ.  A machine backend may emit these records from its
DOS/device adapters; a detached native backend emits them at the equivalent
host boundary.  Guest instruction execution is deliberately not an effect.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass


OBSERVABLE_EFFECT_SCHEMA = "dos-re:observable-effects:v1"

# Stable integer identities.  These are persisted only through their digest,
# but changing a value would still change the contract and therefore requires
# a new OBSERVABLE_EFFECT_SCHEMA.
HARDWARE_INTERRUPT = 1
SOFTWARE_INTERRUPT = 2
PORT_READ = 3
PORT_WRITE = 4
REPLAY_INPUT = 5
SEMANTIC_BOUNDARY = 6
PRESENTATION = 7
FILESYSTEM = 8
CONSOLE_OUTPUT = 9

_RECORD = struct.Struct("<I4xQQQQ")


@dataclass(frozen=True)
class ObservableIntervalDigest:
    """Order-sensitive digest of one half-open replay interval."""

    schema_id: str
    event_count: int
    digest: str

    def __post_init__(self) -> None:
        if not self.schema_id or self.event_count < 0 or len(self.digest) != 64:
            raise ValueError("invalid observable interval digest")


class RollingEffectDigest:
    """Allocation-bounded accumulator for canonical primitive effects.

    ``record`` performs no tuple/dict allocation and reuses one 40-byte buffer.
    ``record_bytes`` is for already-available payloads such as console or file
    output; callers should prefer integer identities in genuinely hot paths.
    """

    __slots__ = ("_hash", "_buffer", "_view", "_count", "_finished")

    def __init__(self, schema_id: str = OBSERVABLE_EFFECT_SCHEMA) -> None:
        if not schema_id:
            raise ValueError("observable-effect schema must not be empty")
        self._hash = hashlib.sha256()
        encoded = schema_id.encode("utf-8")
        self._hash.update(len(encoded).to_bytes(4, "little"))
        self._hash.update(encoded)
        self._buffer = bytearray(_RECORD.size)
        self._view = memoryview(self._buffer)
        self._count = 0
        self._finished = False

    @property
    def event_count(self) -> int:
        return self._count

    def record(
        self,
        kind: int,
        a: int = 0,
        b: int = 0,
        c: int = 0,
        d: int = 0,
    ) -> None:
        if self._finished:
            raise RuntimeError("observable-effect digest is already finished")
        mask = 0xFFFFFFFFFFFFFFFF
        _RECORD.pack_into(
            self._buffer, 0, int(kind) & 0xFFFFFFFF,
            int(a) & mask, int(b) & mask, int(c) & mask, int(d) & mask,
        )
        self._hash.update(self._view)
        self._count += 1

    def record_bytes(
        self,
        kind: int,
        payload: bytes,
        *,
        identity: int = 0,
    ) -> None:
        payload = bytes(payload)
        self.record(kind, identity, len(payload))
        self._hash.update(payload)

    def finish(
        self, schema_id: str = OBSERVABLE_EFFECT_SCHEMA,
    ) -> ObservableIntervalDigest:
        if self._finished:
            raise RuntimeError("observable-effect digest is already finished")
        self._finished = True
        return ObservableIntervalDigest(
            schema_id, self._count, self._hash.hexdigest())
