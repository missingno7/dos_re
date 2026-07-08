"""Back-compat shim: the Display presenter now lives at ``dos_re.display``.

It was promoted into the package by the play-runner unification so game ports
can import it through their installed ``dos_re`` dependency instead of
sys.path-ing into this tools/ directory.  Existing ``from display import
Display`` users keep working through this shim.
"""
from dos_re.display import Display  # noqa: F401
