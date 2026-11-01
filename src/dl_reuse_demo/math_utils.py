"""Simple math utility functions. calculate_average depends on calculate_total,
to give the bundler something to resolve intra-package."""


def calculate_total(values: list[float]) -> float:
    """Sum a list of numbers."""
    return sum(values)


def calculate_average(values: list[float]) -> float:
    """Average a list of numbers. Depends on calculate_total in this module."""
    if not values:
        raise ValueError("values must not be empty")
    return calculate_total(values) / len(values)
