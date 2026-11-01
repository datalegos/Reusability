"""
verify_bundle.py — post-install smoke test for the dlctl self-test bundle.

Run this script inside the fresh venv AFTER installing the generated wheel:

    python tests/verify_bundle.py

Expected output:
    [PASS] function_a result looks correct
    [PASS] function_x result looks correct
    All checks passed.
"""

import sys


def main():
    errors = []

    # --- Test function_a ---
    try:
        from new_bundled_package import function_a
    except ImportError as e:
        errors.append(f"[FAIL] Could not import function_a: {e}")
    else:
        result = function_a(4)
        # helper_a(4) = (4**2) * 3 = 48
        # math.sqrt(4) = 2.0
        # format_greeting("4") = "Hello, 4!"
        expected = {
            "squared_times_multiplier": 48,
            "sqrt": 2.0,
            "greeting": "Hello, 4!",
        }
        if result == expected:
            print("[PASS] function_a result looks correct")
        else:
            errors.append(
                f"[FAIL] function_a returned unexpected result.\n"
                f"       Expected: {expected}\n"
                f"       Got:      {result}"
            )

    # --- Test function_x ---
    try:
        from new_bundled_package import function_x
    except ImportError as e:
        errors.append(f"[FAIL] Could not import function_x: {e}")
    else:
        result = function_x([10, 20, 30])
        # statistics.mean([10,20,30]) = 20.0; + BASE_VALUE(100) = 120.0
        # statistics.stdev([10,20,30]) = 10.0
        # count = 3
        expected_adjusted_mean = 120.0
        expected_stdev = 10.0
        expected_count = 3

        if (
            result.get("adjusted_mean") == expected_adjusted_mean
            and result.get("count") == expected_count
            and abs(result.get("stdev", 0) - expected_stdev) < 1e-9
        ):
            print("[PASS] function_x result looks correct")
        else:
            errors.append(
                f"[FAIL] function_x returned unexpected result.\n"
                f"       Expected: adjusted_mean={expected_adjusted_mean}, "
                f"stdev={expected_stdev}, count={expected_count}\n"
                f"       Got:      {result}"
            )

    # --- Summary ---
    if errors:
        for msg in errors:
            print(msg, file=sys.stderr)
        print(f"\n{len(errors)} check(s) FAILED.", file=sys.stderr)
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
