"""dos_re.lift — automatic literal lifting of 16-bit x86 functions (M0: census).

See docs/lifting_design.md for the full design. This subpackage is the OS-free
part of the lifter: a static decoder (`decode`) self-checkable against the
interpreter, and a function CFG walker (`cfg`) with a structured refusal
taxonomy. Nothing here may import DOS-specific modules (`dos.py`,
`interrupts.py`, ...) — the OS boundary enters later via a policy object, so
this layer stays extractable for a future win16_re.
"""
from .decode import Inst, decode_one  # noqa: F401
from .cfg import FunctionScan, scan_function  # noqa: F401
