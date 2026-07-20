"""CPU-free runtime lookup for bootstrap files in a packaged release."""
from __future__ import annotations

import json
from pathlib import Path


class BootstrapRuntimeError(RuntimeError):
    """The packaged bootstrap manifest is absent, stale, or incomplete."""


def packaged_bootstrap_artifacts(
    product_root: str | Path,
    *,
    expected_provider: str,
) -> dict[str, Path]:
    """Return validated artifact paths from ``dos_re_release.json``."""
    root = Path(product_root)
    manifest_path = root / "dos_re_release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapRuntimeError(
            f"packaged bootstrap manifest is unavailable: {manifest_path}"
        ) from exc
    provider = manifest.get("bootstrap_provider")
    if provider != expected_provider:
        raise BootstrapRuntimeError(
            f"release selected bootstrap {provider!r}, expected "
            f"{expected_provider!r}"
        )
    declared = manifest.get("bootstrap_artifacts")
    if not isinstance(declared, dict):
        raise BootstrapRuntimeError(
            "release manifest has no bootstrap artifact index"
        )
    paths = {
        str(artifact_id): root / str(relative)
        for artifact_id, relative in declared.items()
    }
    missing = sorted(
        artifact_id for artifact_id, path in paths.items()
        if not path.is_file()
    )
    if missing:
        raise BootstrapRuntimeError(
            "packaged bootstrap artifacts are missing: " + ", ".join(missing)
        )
    return paths
