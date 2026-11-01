# dl-reuse-demo

Small, dependency-light demo package used to validate publishing two
independent packages to a private devpi index and installing both with pip.

## Modules

- `dl_reuse_demo.text_utils` — `normalize_text`, `count_words`
- `dl_reuse_demo.math_utils` — `calculate_total`, `calculate_average`
  (`calculate_average` calls `calculate_total` internally, to exercise
  intra-package function composition)

## Installation

```bash
pip install dl-reuse-demo
```

## Usage

```python
from dl_reuse_demo.text_utils import normalize_text, count_words
from dl_reuse_demo.math_utils import calculate_total, calculate_average

normalize_text("  Hello   World  ")   # "hello world"
count_words("Hello World")            # 2
calculate_total([1.0, 2.0, 3.0])      # 6.0
calculate_average([1.0, 2.0, 3.0])    # 2.0
```
