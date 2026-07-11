"""Tests for dos_re.overlay_menu — the native product's in-game settings menu.

pygame is injected into the widget, so the interaction logic tests run against a tiny stub module: no
pygame, no display, no game. (Rendering itself is exercised by the ports' viewers.)"""
from __future__ import annotations

from types import SimpleNamespace

from dos_re.overlay_menu import OverlayMenu, _step_selectable

# a stub 'pygame' with just the key constants handle_keydown reads
PG = SimpleNamespace(
    K_F10=1, K_m=2, K_ESCAPE=3, K_LEFT=4, K_RIGHT=5, K_UP=6, K_DOWN=7,
    K_a=8, K_d=9, K_w=10, K_s=11, K_RETURN=12, K_SPACE=13,
    K_PAGEUP=14, K_PAGEDOWN=15, K_q=16, K_e=17, K_TAB=18,
)


def key(k):
    return SimpleNamespace(key=k)


def make_menu(state):
    """Two tabs: Display (a toggle + an info row + an adjustable) and Experimental (one labeled opt-in)."""
    def tabs():
        return [
            ("Display", [
                {"label": "Fullscreen", "value": "On" if state["fs"] else "Off",
                 "activate": lambda: state.__setitem__("fs", not state["fs"])},
                {"label": "changes apply live", "info": True},
                {"label": "Scale", "value": str(state["scale"]),
                 "adjust": lambda d: state.__setitem__("scale", state["scale"] + d)},
            ]),
            ("Experimental", [
                {"label": "Active zone (affects gameplay)", "value": "On" if state["az"] else "Off",
                 "activate": lambda: state.__setitem__("az", not state["az"])},
            ]),
        ]
    m = OverlayMenu(PG, tabs)
    m.open = True
    return m


def test_step_selectable_skips_info_rows():
    items = [{"label": "a"}, {"label": "note", "info": True}, {"label": "b"}]
    assert _step_selectable(items, 0, 1) == 2          # skips the info row
    assert _step_selectable(items, 2, 1) == 0          # wraps
    only_info = [{"info": True}]
    assert _step_selectable(only_info, 0, 1) == 0      # nothing selectable -> stays put


def test_toggle_fires_and_menu_close_keys():
    state = {"fs": False, "scale": 2, "az": False}
    m = make_menu(state)
    assert m.handle_keydown(key(PG.K_RETURN)) is True  # fire the Fullscreen toggle
    assert state["fs"] is True
    assert m.handle_keydown(key(PG.K_F10)) is False    # F10 closes (returns False)
    assert m.open is False


def test_tab_switch_reaches_experimental_and_fires_optin():
    state = {"fs": False, "scale": 2, "az": False}
    m = make_menu(state)
    assert m.handle_keydown(key(PG.K_RIGHT)) is True   # not editing -> Right switches tab
    assert m.tab == 1
    m.handle_keydown(key(PG.K_SPACE))                  # fire the Experimental opt-in
    assert state["az"] is True


def test_edit_mode_adjusts_and_esc_cancels_before_closing():
    state = {"fs": False, "scale": 2, "az": False}
    m = make_menu(state)
    m.handle_keydown(key(PG.K_DOWN))                   # -> Scale (info row skipped)
    assert m.item == 2
    m.handle_keydown(key(PG.K_RETURN))                 # adjustable -> ENTER edit mode
    assert m.editing is True
    m.handle_keydown(key(PG.K_RIGHT))                  # Right now ADJUSTS (not tab-switch)
    m.handle_keydown(key(PG.K_LEFT))
    m.handle_keydown(key(PG.K_RIGHT))
    assert state["scale"] == 3 and m.tab == 0
    assert m.handle_keydown(key(PG.K_ESCAPE)) is True  # Esc #1: cancel the edit, stay open
    assert m.editing is False and m.open is True
    assert m.handle_keydown(key(PG.K_ESCAPE)) is False  # Esc #2: close
    assert m.open is False


def test_import_needs_no_pygame():
    import sys
    assert "pygame" not in getattr(sys.modules["dos_re.overlay_menu"], "__dict__", {})
