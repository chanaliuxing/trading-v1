def slow_fib(n: int) -> int:
    """Deliberately slow Fibonacci (for evolver experiments)."""
    if n <= 1:
        return n
    return slow_fib(n - 1) + slow_fib(n - 2)


def fast_fib(n: int) -> int:
    """Optimized Fibonacci using iteration."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

