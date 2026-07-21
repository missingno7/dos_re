"""Runtime-safe fail-on-reach witness for incomplete execution graphs.

The planner may permit static uncertainty in a detached development run, but
the runtime must never turn an actual miss into interpreter or EXE fallback.
This exception carries the machine coordinate without importing planner or
Atlas code, so low-level generated carriers can raise it safely.
"""
from __future__ import annotations


class RuntimeExecutionFrontier(RuntimeError):
    """Execution reached code outside the selected runtime implementation."""

    def __init__(
        self,
        *,
        target_address: str,
        edge_kind: str = "instruction-fetch",
        source_identity: str = "",
        target_identity: str = "",
        reason: str = "no selected implementation can execute this target",
    ) -> None:
        self.target_address = target_address
        self.edge_kind = edge_kind
        self.source_identity = source_identity
        self.target_identity = target_identity
        self.reason = reason
        super().__init__(
            f"RUNTIME EXECUTION FRONTIER at {target_address}: {reason}; "
            "interpreter and original-EXE fallback are forbidden"
        )
