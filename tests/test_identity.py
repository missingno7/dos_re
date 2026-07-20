from __future__ import annotations

import pytest

from dos_re.identity import (
    BoundaryIdentity,
    FunctionIdentity,
    ImageIdentity,
    ProgramIdentity,
    RegionIdentity,
    RuntimeCodeSlotIdentity,
    RuntimeCodeVariantIdentity,
    flat_address,
    real_mode_address,
)


def test_identity_is_stable_and_disambiguates_images_and_address_spaces():
    program = ProgramIdentity("game:1.0")
    first = ImageIdentity(program, "main exe", "sha256", "1" * 64)
    second = ImageIdentity(program, "overlay", "sha256", "2" * 64)

    real = FunctionIdentity(first, "real-mode", real_mode_address(0x1010, 0x123))
    overlay = FunctionIdentity(second, "real-mode", real_mode_address(0x1010, 0x123))
    flat = FunctionIdentity(first, "flat32", flat_address(0x10100123))

    assert str(real).endswith(":function:real-mode:1010%3A0123")
    assert len({str(real), str(overlay), str(flat)}) == 3
    assert "%20" in str(first)


def test_runtime_variants_regions_and_boundaries_have_distinct_namespaces():
    program = ProgramIdentity("game:1")
    image = ImageIdentity(program, "exe", "sha1", "a" * 40)
    slot = RuntimeCodeSlotIdentity(
        image, "real-mode", real_mode_address(0x1000, 0x2000))
    variant = RuntimeCodeVariantIdentity(slot, "sha1", "b" * 40)

    assert ":runtime-slot:" in str(slot)
    assert str(variant).startswith(f"{slot}:variant:sha1:")
    assert str(RegionIdentity(program, "main loop")) == "game:1:region:main%20loop"
    assert str(BoundaryIdentity(program, "interrupt", "21")) == \
        "game:1:boundary:interrupt:21"


@pytest.mark.parametrize(
    "call",
    [
        lambda: ImageIdentity(ProgramIdentity("g"), "x", "sha256", "bad"),
        lambda: real_mode_address(-1, 0),
        lambda: flat_address(0x100, width=2),
    ],
)
def test_invalid_identity_components_fail(call):
    with pytest.raises(ValueError):
        call()
