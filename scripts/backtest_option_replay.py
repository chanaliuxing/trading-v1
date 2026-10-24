#!/usr/bin/env python3
"""Generic CLI entrypoint for the isolated option replay engine."""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtest_spy_option_0324 import main


if __name__ == "__main__":
    main()
