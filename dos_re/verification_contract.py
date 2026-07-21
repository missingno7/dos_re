"""Backend-neutral declarations for replay comparison authority.

This module sits below both execution planning and replay verification.  It
contains immutable metadata only: importing it cannot select an
implementation, construct a backend, or inspect a replay artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VerificationRepresentation(str, Enum):
    """The strongest representation a faithful claim compares at one seam."""

    COMPLETE_CONTINUATION = "complete-continuation"
    SEMANTIC_STATE = "semantic-state"
    CONTINUATION_SEAM = "continuation-seam"


@dataclass(frozen=True)
class VerificationProjectionContract:
    """A named, reviewable canonical projection used for one comparison."""

    projection_id: str
    representation: VerificationRepresentation
    schema_id: str
    required_fields: tuple[str, ...] = ()
    required_regions: tuple[str, ...] = ()
    observable_effects: tuple[str, ...] = ()
    excluded_internal_state: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.projection_id or not self.schema_id:
            raise ValueError("verification projection ID and schema must not be empty")
        for label, values in (
            ("required field", self.required_fields),
            ("required region", self.required_regions),
            ("observable effect", self.observable_effects),
            ("excluded internal state", self.excluded_internal_state),
        ):
            if any(not value for value in values) or len(set(values)) != len(values):
                raise ValueError(f"verification {label}s must be non-empty and unique")
        if set(self.required_fields) & set(self.excluded_internal_state):
            raise ValueError(
                "a verification field cannot be both required and excluded"
            )


@dataclass(frozen=True)
class RegionExitVerificationContract:
    """The externally observable contract when an island leaves its owner."""

    exit_id: str
    continuation: str
    projection: VerificationProjectionContract

    def __post_init__(self) -> None:
        if not self.exit_id or not self.continuation:
            raise ValueError("region exit verification needs an exit and continuation")
        if self.projection.representation is not VerificationRepresentation.CONTINUATION_SEAM:
            raise ValueError(
                "region exit verification must use a continuation-seam projection"
            )


@dataclass(frozen=True)
class RegionVerificationContract:
    """Interior semantic evidence plus every declared external exit seam."""

    contract_id: str
    interior: VerificationProjectionContract
    exits: tuple[RegionExitVerificationContract, ...]

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("region verification contract ID must not be empty")
        if self.interior.representation is VerificationRepresentation.CONTINUATION_SEAM:
            raise ValueError("region interior cannot use a continuation-seam projection")
        if not self.exits:
            raise ValueError("region verification contract needs every exit seam")
        exit_ids = [item.exit_id for item in self.exits]
        if len(set(exit_ids)) != len(exit_ids):
            raise ValueError("region exit verification IDs must be unique")


def region_verification_payload(
    contract: RegionVerificationContract | None,
) -> dict[str, object] | None:
    """Stable materialization for plans, exports, and verification reports."""

    if contract is None:
        return None

    def projection(item: VerificationProjectionContract) -> dict[str, object]:
        return {
            "id": item.projection_id,
            "representation": item.representation.value,
            "schema": item.schema_id,
            "required_fields": list(item.required_fields),
            "required_regions": list(item.required_regions),
            "observable_effects": list(item.observable_effects),
            "excluded_internal_state": list(item.excluded_internal_state),
        }

    return {
        "id": contract.contract_id,
        "interior": projection(contract.interior),
        "exits": [
            {
                "exit_id": item.exit_id,
                "continuation": item.continuation,
                "projection": projection(item.projection),
            }
            for item in contract.exits
        ],
    }
