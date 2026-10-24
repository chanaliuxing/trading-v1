"""
Tests for demo.py
"""
import pytest
from demo import foo, bar, calculate_sum


def test_foo():
    """Test foo function."""
    assert foo(1) == 2
    assert foo(0) == 1
    assert foo(-1) == 0


def test_bar():
    """Test bar function."""
    assert bar(3) == 6
    assert bar(0) == 0
    assert bar(-2) == -4


def test_calculate_sum():
    """Test calculate_sum function."""
    assert calculate_sum(1, 2) == 3
    assert calculate_sum(0, 0) == 0
    assert calculate_sum(-1, 1) == 0
    assert calculate_sum(100, 200) == 300
