"""Deterministic product feature state for planned dos_re compositions.

Features are not implementations and do not participate in target ownership.
The execution plan authorizes them; this controller only queues replayable
changes and applies them at descriptor-declared safe boundaries.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .execution import FeatureCategory, FeatureDescriptor


FEATURE_EVENT_SCHEMA = "dos_re.feature-event/v1"


class FeaturePolicyError(RuntimeError):
    """A feature request violates the immutable product plan."""


@dataclass(frozen=True)
class PendingFeatureChange:
    feature_id: str
    value: Any


class FeatureController:
    """Small deterministic state machine shared by live and replay input.

    Behavioral changes requested during live execution must be recorded.  A
    replay feeds the same payload through :meth:`accept_replay_event`; neither
    path can apply it until a declared safe boundary is reached.
    """

    def __init__(self, features: Iterable[FeatureDescriptor]) -> None:
        descriptors = tuple(features)
        self._features = {item.feature_id: item for item in descriptors}
        if len(self._features) != len(descriptors):
            raise ValueError("planned feature IDs must be unique")
        self._state = {
            item.feature_id: item.default_value for item in descriptors
        }
        self._pending: list[PendingFeatureChange] = []

    @property
    def state(self) -> Mapping[str, Any]:
        return dict(self._state)

    @property
    def pending(self) -> tuple[PendingFeatureChange, ...]:
        return tuple(self._pending)

    def request(
        self,
        feature_id: str,
        value: Any,
        *,
        ordinal: int,
        record_event: Callable[[int, str, Any], object] | None = None,
    ) -> dict[str, Any]:
        descriptor = self._descriptor(feature_id)
        payload = {
            "schema": FEATURE_EVENT_SCHEMA,
            "feature_id": feature_id,
            "value": value,
        }
        if descriptor.category is FeatureCategory.BEHAVIORAL:
            if record_event is None:
                raise FeaturePolicyError(
                    f"behavioral feature {feature_id!r} must be recorded"
                )
            record_event(ordinal, descriptor.replay_channel, payload)
        elif record_event is not None and descriptor.replay_channel:
            record_event(ordinal, descriptor.replay_channel, payload)
        self._pending.append(PendingFeatureChange(feature_id, value))
        return payload

    def accept_replay_event(self, channel: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) \
                or payload.get("schema") != FEATURE_EVENT_SCHEMA:
            raise FeaturePolicyError("invalid feature replay event payload")
        feature_id = str(payload.get("feature_id", ""))
        descriptor = self._descriptor(feature_id)
        if channel != descriptor.replay_channel:
            raise FeaturePolicyError(
                f"feature {feature_id!r} uses replay channel "
                f"{descriptor.replay_channel!r}, not {channel!r}"
            )
        self._pending.append(PendingFeatureChange(
            feature_id, payload.get("value")
        ))

    def apply_pending(
        self,
        boundary: str,
        apply: Callable[[str, Any], None],
    ) -> tuple[PendingFeatureChange, ...]:
        applied: list[PendingFeatureChange] = []
        retained: list[PendingFeatureChange] = []
        for change in self._pending:
            descriptor = self._features[change.feature_id]
            if descriptor.safe_boundaries \
                    and boundary not in descriptor.safe_boundaries:
                retained.append(change)
                continue
            apply(change.feature_id, change.value)
            self._state[change.feature_id] = change.value
            applied.append(change)
        self._pending = retained
        return tuple(applied)

    def _descriptor(self, feature_id: str) -> FeatureDescriptor:
        try:
            return self._features[feature_id]
        except KeyError as exc:
            raise FeaturePolicyError(
                f"feature {feature_id!r} is not enabled by the execution plan"
            ) from exc
