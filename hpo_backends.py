# -*- coding: utf-8 -*-
"""
Pluggable HPO backends for production use.

Currently implemented:
- Optuna + TPE (robust default)
- Optuna + BoTorch (Gaussian Process BO via GPyTorch)  [optional, if installed]

You can add more by implementing `build_sampler()` in a new backend class.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Any, Dict

import random

try:
    import numpy as np
except Exception:
    np = None

import optuna
from optuna.samplers import TPESampler

# Optional backends
try:
    from optuna.integration import BoTorchSampler  # type: ignore
    _HAS_BOTORCH = True
except Exception:
    BoTorchSampler = None
    _HAS_BOTORCH = False


@dataclass
class BackendConfig:
    name: str = "optuna-tpe"           # 'optuna-tpe' | 'optuna-botorch'
    seed: Optional[int] = 2025
    tpe_n_startup_trials: int = 20
    tpe_n_ei_candidates: int = 24
    botorch_gp_type: str = "saasbo"    # 'saasbo' | 'gp'
    botorch_n_startup_trials: int = 24


class SearchBackend:
    """Return (sampler, sampler_info: dict) for an Optuna Study."""
    def __init__(self, cfg: BackendConfig):
        self.cfg = cfg

    def build_sampler(self):
        raise NotImplementedError


class OptunaTPEBackend(SearchBackend):
    def build_sampler(self):
        sampler = TPESampler(
            seed=self.cfg.seed,
            n_startup_trials=self.cfg.tpe_n_startup_trials,
            n_ei_candidates=self.cfg.tpe_n_ei_candidates,
            multivariate=True,
            group=True,
        )
        return sampler, {"backend": "optuna-tpe"}


class OptunaBoTorchBackend(SearchBackend):
    def build_sampler(self):
        if not _HAS_BOTORCH:
            # Fallback to TPE if BoTorch isn't available
            tpe = TPESampler(
                seed=self.cfg.seed,
                n_startup_trials=self.cfg.tpe_n_startup_trials,
                n_ei_candidates=self.cfg.tpe_n_ei_candidates,
                multivariate=True,
                group=True,
            )
            return tpe, {"backend": "optuna-tpe", "note": "BoTorch unavailable; fell back to TPE"}
        # Build BoTorch Sampler
        sampler = BoTorchSampler(
            n_startup_trials=self.cfg.botorch_n_startup_trials,
            # Choose SAASBO for high-dim sparse spaces. Otherwise vanilla GP.
            # Optuna will select the proper model internally based on the search space.
        )
        return sampler, {"backend": "optuna-botorch", "gp": self.cfg.botorch_gp_type}


def make_backend(name: str, **kwargs) -> SearchBackend:
    name = (name or "optuna-tpe").lower()
    cfg = BackendConfig(name=name, **kwargs)
    if name in ("optuna", "optuna-tpe", "tpe"):
        return OptunaTPEBackend(cfg)
    elif name in ("optuna-botorch", "botorch", "gp"):
        return OptunaBoTorchBackend(cfg)
    else:
        # default
        return OptunaTPEBackend(cfg)
