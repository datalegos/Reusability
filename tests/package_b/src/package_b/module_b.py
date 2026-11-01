"""
package_b.module_b — main module containing the functions we'll select.

Demonstrates:
  - A function that uses a module-level constant (BASE_VALUE).
  - A function that calls a same-module helper (helper_x).
  - Standard-library usage (statistics) — kept as stdlib import.
  - No dependency on package_a, so there's no cross-package coupling warning.
"""

import statistics

# A module-level constant used by function_x
BASE_VALUE = 100


def helper_x(values: list) -> float:
    """Private helper: returns the mean of a list of numbers plus BASE_VALUE.

    Pulled in transitively when function_x is selected.
    """
    return statistics.mean(values) + BASE_VALUE


def function_x(values: list) -> dict:
    """Selected function X.

    Computes a small statistics summary that exercises:
      - helper_x (same-module helper → pulled in transitively)
      - BASE_VALUE (same-module constant → pulled in transitively)
      - statistics.mean / statistics.stdev (stdlib → kept as import, not bundled)

    Args:
        values: A non-empty list of numbers.

    Returns:
        A dict with keys 'adjusted_mean', 'stdev', and 'count'.
    """
    return {
        "adjusted_mean": helper_x(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "count": len(values),
    }


def function_y(a: int, b: int) -> int:
    """Another function in this module (NOT selected in the self-test).

    Simple addition — only here to verify non-selected functions are excluded.
    """
    return a + b
