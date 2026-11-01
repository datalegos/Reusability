"""
package_a.utils — shared helpers used by the main module.

This module is a sibling module; dlctl will copy it wholesale when any
function in package_a.module_a depends on something defined here.
"""

# A simple constant used by helpers in this module
GREETING_PREFIX = "Hello"


def format_greeting(name: str) -> str:
    """Return a formatted greeting string."""
    return f"{GREETING_PREFIX}, {name}!"
