"""Physical-pixel display setup is deterministic and degrades cleanly."""
from __future__ import annotations

from dos_re.display_platform import configure_physical_pixel_coordinates


class _API:
    def __init__(self, calls, name, *, fail=False):
        self.calls = calls
        self.name = name
        self.fail = fail

    def _call(self, method, value=None):
        self.calls.append((self.name, method, value))
        if self.fail:
            raise AttributeError(method)

    def SetProcessDpiAwarenessContext(self, value):
        self._call("context", value)

    def SetProcessDpiAwareness(self, value):
        self._call("awareness", value)

    def SetProcessDPIAware(self):
        self._call("legacy")


class _WinDLL:
    def __init__(self, calls, *, user32_fail=False, shcore_fail=False):
        self.user32 = _API(calls, "user32", fail=user32_fail)
        self.shcore = _API(calls, "shcore", fail=shcore_fail)


def test_non_windows_does_not_change_environment():
    env = {}
    result = configure_physical_pixel_coordinates(
        platform="linux", environ=env, windll=object(),
    )
    assert result == "not-windows"
    assert env == {}


def test_windows_prefers_per_monitor_v2_and_sets_sdl_hint():
    calls = []
    env = {}
    result = configure_physical_pixel_coordinates(
        platform="win32",
        environ=env,
        windll=_WinDLL(calls),
        dpi_context="PER_MONITOR_V2",
    )
    assert result == "per-monitor-v2"
    assert env == {"SDL_WINDOWS_DPI_AWARENESS": "permonitorv2"}
    assert calls == [("user32", "context", "PER_MONITOR_V2")]


def test_windows_falls_back_without_overwriting_explicit_sdl_policy():
    calls = []
    env = {"SDL_WINDOWS_DPI_AWARENESS": "system"}
    result = configure_physical_pixel_coordinates(
        platform="win32",
        environ=env,
        windll=_WinDLL(calls, user32_fail=True),
    )
    assert result == "per-monitor"
    assert env["SDL_WINDOWS_DPI_AWARENESS"] == "system"
    assert calls == [
        ("user32", "context", -4),
        ("shcore", "awareness", 2),
    ]


def test_windows_uses_legacy_api_as_the_final_fallback():
    calls = []
    result = configure_physical_pixel_coordinates(
        platform="win32",
        environ={},
        windll=_WinDLL(calls, user32_fail=True, shcore_fail=True),
    )
    assert result == "unavailable"
    assert calls == [
        ("user32", "context", -4),
        ("shcore", "awareness", 2),
        ("user32", "legacy", None),
    ]
