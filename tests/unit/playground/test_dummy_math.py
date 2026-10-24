from libs.playground.dummy_math import slow_fib, fast_fib


def test_fib_small_values():
    cases = [(0, 0), (1, 1), (2, 1), (3, 2), (4, 3), (5, 5), (6, 8)]
    for n, expected in cases:
        assert slow_fib(n) == expected
        assert fast_fib(n) == expected


def test_fast_is_faster_for_larger_n():
    # Not a strict perf test; just sanity check correctness on a larger n
    assert fast_fib(20) == 6765
    assert slow_fib(10) == 55  # still small enough to compute quickly

