"""Host display setup that must happen before an SDL window is created.

Windows reports DPI-virtualized desktop coordinates to processes that are not
DPI-aware.  At 150% scaling, for example, a physical 3840x2160 monitor appears
to be 2560x1440.  A borderless desktop window built from those coordinates
therefore covers only part of the monitor.

Keep this setup independent of pygame so every frontend can apply it before
initializing SDL, regardless of which presentation backend it selects.
"""
from __future__ import annotations

import os
import sys
from typing import Any, MutableMapping


def configure_physical_pixel_coordinates(
    *,
    platform: str | None = None,
    environ: MutableMapping[str, str] | None = None,
    windll: Any = None,
    dpi_context: Any = None,
) -> str:
    """Make Windows/SDL use physical monitor pixels.

    Returns the Win32 API tier that accepted the request.  Failure is
    deliberately non-fatal: old Windows versions and non-Windows hosts retain
    their normal SDL behaviour.

    ``windll`` and ``dpi_context`` are injectable solely so the fallback order
    can be tested without changing the test runner's real process DPI mode.
    """
    host = sys.platform if platform is None else platform
    if host != "win32":
        return "not-windows"

    env = os.environ if environ is None else environ
    # SDL reads this when its video subsystem is initialized.  setdefault
    # preserves an explicit host/application choice.
    env.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")

    if windll is None:
        try:
            import ctypes
            windll = ctypes.windll
            dpi_context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except Exception:  # noqa: BLE001 - optional best-effort host setup
            return "unavailable"
    elif dpi_context is None:
        dpi_context = -4

    try:
        # Windows 10: physical pixels and correct behaviour when moving a
        # window between monitors with different scale factors.
        windll.user32.SetProcessDpiAwarenessContext(dpi_context)
        return "per-monitor-v2"
    except Exception:  # noqa: BLE001 - API is absent on older Windows
        pass

    try:
        # Windows 8.1 fallback. PROCESS_PER_MONITOR_DPI_AWARE == 2.
        windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:  # noqa: BLE001 - API is absent on older Windows
        pass

    try:
        # Vista-era fallback: still prevents logical desktop virtualization,
        # although it cannot adapt when crossing monitors.
        windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001 - no usable Win32 DPI API
        return "unavailable"
