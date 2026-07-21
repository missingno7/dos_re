"""Closed-world serialization of a selected execution graph.

The development planner produces this file.  A product launcher or native
build generator can consume the plain JSON without importing the planner or
performing runtime implementation selection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .execution import ExecutionPlan


MATERIALIZED_PLAN_SCHEMA = "dos_re.execution-plan/v3"


def materialized_plan_payload(plan: "ExecutionPlan") -> dict[str, Any]:
    implementations = {
        item.implementation_id: item for item in plan.implementations
    }
    entries = {
        item.descriptor.implementation_id: item for item in plan.catalog.entries
    }
    selected_ids = sorted(implementations)
    carrier = plan.report.execution_carrier
    return {
        "schema": MATERIALIZED_PLAN_SCHEMA,
        "plan_digest": plan.plan_digest,
        "program_identity": plan.configuration.program_identity,
        "profile": plan.configuration.profile,
        "product_profile": plan.configuration.product_profile,
        "closure_policy": plan.configuration.execution_policy.closure.value,
        "fallback_policy": plan.configuration.execution_policy.fallback.value,
        "execution_carrier": carrier,
        "bootstrap_provider": plan.bootstrap_provider.provider_id,
        "bindings": {
            item.target: item.implementation_id for item in plan.bindings
        },
        "implementations": {
            implementation_id: {
                "digest": implementations[implementation_id].implementation_digest,
                "origin": implementations[implementation_id].origin.value,
                "category": implementations[implementation_id].category.value,
                "recovery_level": (
                    None
                    if implementations[implementation_id].recovery_level is None
                    else implementations[implementation_id].recovery_level.value
                ),
                "contract": (
                    None if implementations[implementation_id].contract is None
                    else implementations[implementation_id].contract.contract_id
                ),
                "region": implementations[implementation_id].region_id,
                "adapter": next((
                    {
                        "id": adapter.adapter_id,
                        "digest": adapter.adapter_digest,
                    }
                    for adapter in entries[implementation_id].adapters
                    if adapter.carrier_id == carrier
                ), None),
            }
            for implementation_id in selected_ids
        },
        "regions": {
            item.region_id: {
                "implementation": item.implementation_id,
                "host_carrier": item.host_carrier_id,
                "region_carrier": item.region_carrier_id,
                "adapter": {
                    "id": item.adapter_id,
                    "digest": item.adapter_digest,
                },
                "state_ownership": item.state_ownership.value,
                "entries": {
                    entry.entry_id: entry.target for entry in item.entries
                },
                "exits": {
                    exit.exit_id: exit.continuation for exit in item.exits
                },
                "covered_targets": list(item.covered_targets),
                "suppressed_bindings": {
                    binding.target: binding.implementation_id
                    for binding in item.suppressed_bindings
                },
                "replay_boundaries": list(item.replay_boundaries),
            }
            for item in plan.regions
        },
        "features": {
            item.feature_id: {
                "digest": item.feature_digest,
                "category": item.category.value,
                "replay_channel": item.replay_channel,
                "safe_boundaries": sorted(item.safe_boundaries),
                "default_value": item.default_value,
            }
            for item in plan.features
        },
        "services": {
            item.service_id: item.implementation_digest for item in plan.services
        },
    }


def write_materialized_plan(plan: "ExecutionPlan", path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            materialized_plan_payload(plan),
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def load_materialized_plan(path: str | Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != MATERIALIZED_PLAN_SCHEMA:
        raise ValueError(
            f"unsupported materialized execution plan: {payload.get('schema')!r}"
        )
    return payload
