"""Production runtime; the dependency-free local demonstration remains separate."""

import sys

if sys.version_info < (3, 12):
    raise RuntimeError("The production runtime requires Python 3.12 or newer")
