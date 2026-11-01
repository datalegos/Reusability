from dl_reuse_demo.text_utils import normalize_text, count_words
from dl_reuse_demo.math_utils import calculate_total, calculate_average


def test_normalize_text():
    assert normalize_text("  Hello   World  ") == "hello world"


def test_count_words():
    assert count_words("Hello World") == 2


def test_calculate_total():
    assert calculate_total([1.0, 2.0, 3.0]) == 6.0


def test_calculate_average():
    assert calculate_average([1.0, 2.0, 3.0]) == 2.0
