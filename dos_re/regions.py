"""Runtime-neutral control handoff for long-lived execution islands.

The planner owns selection and contracts.  A port-side region adapter creates
one session when its declared entry seam is reached.  Player/backend code then
advances that session at semantic replay boundaries until it returns one of the
declared exits.  The dispatcher validates the route but knows nothing about CPU
registers, DOS memory, native state, or game-specific transition mechanics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .execution import RegionExitPoint, ResolvedExecutionRegion


@dataclass(frozen=True)
class RegionProgress:
    """One semantic yield or terminal exit from an active region session."""

    boundary_id: str = ""
    exit_id: str = ""

    def __post_init__(self) -> None:
        if bool(self.boundary_id) == bool(self.exit_id):
            raise ValueError(
                "region progress must contain exactly one boundary or exit ID"
            )

    @classmethod
    def yielded(cls, boundary_id: str) -> "RegionProgress":
        return cls(boundary_id=boundary_id)

    @classmethod
    def exited(cls, exit_id: str) -> "RegionProgress":
        return cls(exit_id=exit_id)


class RegionSession(Protocol):
    """Backend-neutral active island. State remains owned by its adapter."""

    def advance(self) -> RegionProgress: ...


class RegionHandoff(RuntimeError):
    """Unwind the surrounding carrier after an island acquires control."""

    def __init__(self, region_id: str, entry_id: str):
        self.region_id = region_id
        self.entry_id = entry_id
        super().__init__(f"entered execution region {region_id!r} at {entry_id!r}")


@dataclass
class _ActiveRegion:
    binding: ResolvedExecutionRegion
    entry_id: str
    session: RegionSession
    complete: Callable[[RegionExitPoint], None]


class RegionDispatcher:
    """Own at most one active island and enforce its materialized contract."""

    def __init__(self) -> None:
        self._active: _ActiveRegion | None = None
        self.last_region_id = ""
        self.last_entry_id = ""
        self.last_exit_id = ""

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_region_id(self) -> str:
        return "" if self._active is None else self._active.binding.region_id

    def enter(
        self,
        binding: ResolvedExecutionRegion,
        entry_id: str,
        session: RegionSession,
        *,
        complete: Callable[[RegionExitPoint], None],
    ) -> None:
        if self._active is not None:
            raise RuntimeError(
                f"cannot enter {binding.region_id!r}; region "
                f"{self._active.binding.region_id!r} already owns execution"
            )
        if entry_id not in {item.entry_id for item in binding.entries}:
            raise RuntimeError(
                f"region {binding.region_id!r} does not declare entry {entry_id!r}"
            )
        self._active = _ActiveRegion(binding, entry_id, session, complete)
        self.last_region_id = binding.region_id
        self.last_entry_id = entry_id
        self.last_exit_id = ""

    def handoff(
        self,
        binding: ResolvedExecutionRegion,
        entry_id: str,
        session: RegionSession,
        *,
        complete: Callable[[RegionExitPoint], None],
    ) -> None:
        """Enter and unwind the surrounding carrier to its frame driver."""
        self.enter(binding, entry_id, session, complete=complete)
        raise RegionHandoff(binding.region_id, entry_id)

    def advance(self) -> RegionProgress:
        active = self._active
        if active is None:
            raise RuntimeError("no execution region is active")
        progress = active.session.advance()
        if progress.boundary_id:
            allowed = set(active.binding.replay_boundaries)
            if progress.boundary_id not in allowed:
                raise RuntimeError(
                    f"region {active.binding.region_id!r} yielded undeclared "
                    f"replay boundary {progress.boundary_id!r}"
                )
            return progress
        exits = {item.exit_id: item for item in active.binding.exits}
        exit_point = exits.get(progress.exit_id)
        if exit_point is None:
            raise RuntimeError(
                f"region {active.binding.region_id!r} returned undeclared "
                f"exit {progress.exit_id!r}"
            )
        active.complete(exit_point)
        self.last_exit_id = progress.exit_id
        self._active = None
        return progress


def ensure_region_dispatcher(runtime: object) -> RegionDispatcher:
    dispatcher = getattr(runtime, "execution_regions", None)
    if dispatcher is None:
        dispatcher = RegionDispatcher()
        setattr(runtime, "execution_regions", dispatcher)
    if not isinstance(dispatcher, RegionDispatcher):
        raise TypeError("runtime.execution_regions is not a RegionDispatcher")
    return dispatcher

