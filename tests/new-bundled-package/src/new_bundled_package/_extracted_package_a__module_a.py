# Extracted by dlctl bundle
# Source package : package_a
# Source module  : package_a.module_a
# Source file    : D:\dlctl-package\tests\package_a\src\package_a\module_a.py
# Function       : function_a
# Generated at   : 2026-09-02T06:32:22.233375+00:00
#
# This file contains the target function plus every definition and import
# it transitively depends on from the source module.
# Do not edit manually -- re-run `dlctl bundle` to regenerate.

import math
from ._local_package_a__utils import format_greeting


def helper_a(x: int) -> int:
    """Private helper: squares x and multiplies by MULTIPLIER.

    This helper is pulled in transitively when function_a is selected
    because function_a calls helper_a().
    """
    return x ** 2 * MULTIPLIER
MULTIPLIER = 3


def function_a(x: int) -> dict:
    """Selected function A.

    Computes a small result dict that exercises:
      - helper_a (same-module helper → pulled in transitively)
      - MULTIPLIER (same-module constant → pulled in transitively)
      - math.sqrt (stdlib import → kept as import, not bundled)
      - format_greeting (sibling module import → sibling copied wholesale)

    Args:
        x: Any integer.

    Returns:
        A dict with keys 'squared_times_multiplier', 'sqrt', and 'greeting'.
    """
    return {'squared_times_multiplier': helper_a(x), 'sqrt': math.sqrt(x), 'greeting': format_greeting(str(x))}
