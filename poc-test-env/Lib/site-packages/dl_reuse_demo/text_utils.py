"""Simple text utility functions with no external dependencies."""


def normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase the input text."""
    return " ".join(text.split()).lower()


def count_words(text: str) -> int:
    """Count the number of whitespace-separated words in the text."""
    return len(text.split())
