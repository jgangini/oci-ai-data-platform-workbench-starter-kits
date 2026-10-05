"""Compatibility import for deployed integration scripts; use territorial_bootstrap."""
import sys
import territorial_bootstrap as _implementation

_implementation.bootstrap_prisma = _implementation.bootstrap_territorial
sys.modules[__name__] = _implementation
