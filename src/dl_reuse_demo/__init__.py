"""
dl-reuse-demo
=============
Small demo package used to validate publishing/installing two independent
packages from a private devpi index.
"""

from .text_utils import normalize_text, count_words
from .math_utils import calculate_total, calculate_average

__version__ = "1.0.0"
__all__ = [
    "normalize_text",
    "count_words",
    "calculate_total",
    "calculate_average",
]
