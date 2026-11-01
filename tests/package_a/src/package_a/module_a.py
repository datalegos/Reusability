"""
package_a.module_a — main module containing the functions we'll select.

Demonstrates:
  - A function that uses a module-level constant (MULTIPLIER).
  - A function that calls a helper defined in the same module (helper_a).
  - A function that uses a sibling module (package_a.utils) via an import.
  - Standard-library usage (math) — should be kept as a stdlib import, not bundled.
"""

import math
from package_a.utils import format_greeting

# A module-level constant used by function_a
MULTIPLIER = 3


def helper_a(x: int) -> int:
    """Private helper: squares x and multiplies by MULTIPLIER.

    This helper is pulled in transitively when function_a is selected
    because function_a calls helper_a().
    """
    return (x ** 2) * MULTIPLIER


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
    return {
        "squared_times_multiplier": helper_a(x),
        "sqrt": math.sqrt(x),
        "greeting": format_greeting(str(x)),
    }


def function_b(values: list) -> float:
    """Another function in this module (NOT selected in the self-test).

    Uses math.fsum — only here to show that non-selected functions are excluded.
    """
    return math.fsum(values)
