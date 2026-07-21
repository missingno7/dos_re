"""Explicit projection contracts prevent semantic verification opt-outs."""
from __future__ import annotations

from dos_re.execution import (
    VerificationProjectionContract,
    VerificationRepresentation,
)
from dos_re.replay import (
    CanonicalState,
    compare_projection_contract,
    projection_contract_diagnostics,
)


CONTRACT = VerificationProjectionContract(
    "test:gameplay-semantic/v1",
    VerificationRepresentation.SEMANTIC_STATE,
    "test:semantic/v1",
    required_fields=("gameplay.rng", "timeline.tick", "audio.claim"),
    required_regions=("scene",),
    observable_effects=("audio:opl-command-stream",),
    excluded_internal_state=("cpu.registers", "guest.stack-scratch"),
)


def _state(*, rng: int = 7, include_audio: bool = True) -> CanonicalState:
    fields = {
        "gameplay": {"rng": rng},
        "timeline": {"tick": 4},
    }
    if include_audio:
        fields["audio"] = {"claim": "opl-command-stream"}
    return CanonicalState(
        "test:semantic/v1", 3, fields, {"scene": b"scene"},
    )


def test_contract_compares_required_semantics_and_ignores_declared_carrier_noise() -> None:
    oracle = _state()
    candidate = _state()

    compared = compare_projection_contract(oracle, candidate, CONTRACT)

    assert compared.equivalent
    assert projection_contract_diagnostics(CONTRACT)[-1] == (
        "excluded internal state: cpu.registers, guest.stack-scratch"
    )


def test_contract_rejects_shared_omission_and_deliberate_authoritative_divergence() -> None:
    omitted = compare_projection_contract(
        _state(include_audio=False), _state(include_audio=False), CONTRACT)
    assert not omitted.equivalent
    assert "omits required field 'audio.claim'" in omitted.differences[0]

    divergent = compare_projection_contract(_state(), _state(rng=8), CONTRACT)
    assert not divergent.equivalent
    assert "fields.gameplay.rng" in divergent.differences[0]
