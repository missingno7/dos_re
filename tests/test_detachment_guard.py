"""Dynamic detachment guard tests."""
from __future__ import annotations

import builtins

import pytest

from dos_re.detachment_guard import (
    BASE_FORBIDDEN,
    DetachedDependencyError,
    forbidden_hit,
    import_guard,
    install_import_guard,
    resolve_import,
    uninstall_import_guard,
)


def test_forbidden_hit_is_package_prefix_matched():
    assert forbidden_hit("dos_re.cpu", BASE_FORBIDDEN) == "dos_re.cpu"
    assert forbidden_hit("dos_re.cpu.sub", BASE_FORBIDDEN) == "dos_re.cpu"
    assert forbidden_hit("dos_re.cpuxyz", BASE_FORBIDDEN) is None
    assert forbidden_hit("dos_re.memory", BASE_FORBIDDEN) is None
    assert forbidden_hit(
        "port.adapters.x", ("port.adapters",)) == "port.adapters"


def test_relative_imports_are_resolved_before_policy_check():
    globals_ = {"__package__": "dos_re.lift"}
    assert resolve_import("cpu", globals_, 1) == "dos_re.lift.cpu"
    assert resolve_import("cpu", globals_, 2) == "dos_re.cpu"
    assert resolve_import("dos_re.cpu", globals_, 0) == "dos_re.cpu"


def test_guard_refuses_base_and_project_dependencies():
    with import_guard(extra_forbidden=("myport.generated_adapters",)):
        with pytest.raises(DetachedDependencyError):
            __import__("dos_re.cpu")
        with pytest.raises(DetachedDependencyError):
            __import__("myport.generated_adapters")
        __import__("json")


def test_uninstall_is_idempotent_and_nested_guards_unwind():
    original = builtins.__import__
    install_import_guard()
    outer = builtins.__import__
    install_import_guard()
    assert builtins.__import__ is not outer
    assert uninstall_import_guard() is True
    assert builtins.__import__ is outer
    assert uninstall_import_guard() is True
    assert builtins.__import__ is original
    assert uninstall_import_guard() is False


def test_context_restores_import_function_after_failure():
    original = builtins.__import__
    with pytest.raises(DetachedDependencyError):
        with import_guard():
            __import__("dos_re.cpu")
    assert builtins.__import__ is original

