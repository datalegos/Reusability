# Extracted by dlctl bundle
# Source package : package_b
# Source module  : package_b.module_b
# Source file    : D:\dlctl-package\tests\package_b\src\package_b\module_b.py
# Function       : function_x
# Generated at   : 2026-09-02T06:32:22.234368+00:00
#
# This file contains the target function plus every definition and import
# it transitively depends on from the source module.
# Do not edit manually -- re-run `dlctl bundle` to regenerate.

import statistics


def helper_x(values: list) -> float:
    """Private helper: returns the mean of a list of numbers plus BASE_VALUE.

    Pulled in transitively when function_x is selected.
    """
    return statistics.mean(values) + BASE_VALUE
BASE_VALUE = 100


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
    return {'adjusted_mean': helper_x(values), 'stdev': statistics.stdev(values) if len(values) > 1 else 0.0, 'count': len(values)}
