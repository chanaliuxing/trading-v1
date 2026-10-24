# -*- coding: utf-8 -*-
"""
wf_hpo_adapter.py

A minimal, production-friendly "runner adapter" that lets the HPO CLI call *your* backtest/evaluator.

Usage patterns:
- Provide --runner "my_pkg.my_module:run_trial" on CLI,
  where run_trial(config: dict, params: dict, fold: dict|None) -> dict
  and returns a dict with keys containing your objective metric (e.g. 'sharpe', 'mdd', 'pnl', 'trades').

This adapter dynamically imports and caches the callable.

If you already have a function in your repo (e.g., wf_hpo.evaluate_trial),
you can simply pass --runner "wf_hpo:evaluate_trial".
"""

from __future__ import annotations
import importlib
from functools import lru_cache
from typing import Callable, Dict, Optional


@lru_cache(maxsize=8)
def _load_runner(path: str) -> Callable:
    """
    path: "module.submodule:function"
    """
    if ":" not in path:
        raise ValueError(f"Invalid --runner '{path}'. Use 'module.submodule:function'.")
    mod_path, func_name = path.split(":", 1)
    mod = importlib.import_module(mod_path)
    fn = getattr(mod, func_name, None)
    if fn is None:
        raise AttributeError(f"Function '{func_name}' not found in module '{mod_path}'.")
    return fn


def evaluate_with_runner(runner_path: str, config: Dict, params: Dict, fold: Optional[Dict] = None) -> Dict:
    fn = _load_runner(runner_path)
    return fn(config, params, fold)
