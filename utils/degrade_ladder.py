"""
Degradation Ladder for Alpha Loop
----------------------------------
Progressively reduces resource usage when memory/time budget is threatened.

Usage:
    ladder = DegradeLadder()
    config = ladder.apply(round_idx=1, failure_reason="mem_guard")
    # Returns degraded configuration based on failure round
"""

from typing import Dict, Any, List
import structlog

logger = structlog.get_logger(__name__)


class DegradeLadder:
    """Progressive resource degradation based on failure rounds."""

    def __init__(self):
        # Define degradation levels
        self.levels = [
            {  # Level 0 (baseline - no degradation)
                "level": 0,
                "symbols_limit": None,  # No limit
                "lookback_days": None,  # Use config default
                "folds": None,  # Use config default
                "n_jobs": 4,
                "gens": None,  # Use CLI arg
                "n_trials": None,  # Use config
                "grid_max_points": 300,
                "description": "Baseline (no degradation)"
            },
            {  # Level 1 (mild degradation)
                "level": 1,
                "symbols_limit": 3,
                "lookback_days": 45,
                "folds": 4,
                "n_jobs": 2,
                "gens": 2,
                "n_trials": 60,
                "grid_max_points": 200,
                "description": "Mild: 3 symbols, 45d lookback, 4 folds, n_jobs=2"
            },
            {  # Level 2 (moderate degradation)
                "level": 2,
                "symbols_limit": 2,
                "lookback_days": 30,
                "folds": 3,
                "n_jobs": 1,
                "gens": 2,
                "n_trials": 40,
                "grid_max_points": 150,
                "description": "Moderate: 2 symbols, 30d lookback, 3 folds, n_jobs=1"
            },
            {  # Level 3 (aggressive degradation)
                "level": 3,
                "symbols_limit": 1,
                "lookback_days": 21,
                "folds": 2,
                "n_jobs": 1,
                "gens": 1,
                "n_trials": 20,
                "grid_max_points": 100,
                "description": "Aggressive: 1 symbol, 21d lookback, 2 folds, minimal grid"
            }
        ]

    def apply(self, round_idx: int, failure_reason: str = "unknown") -> Dict[str, Any]:
        """
        Get degradation configuration for a given round.

        Args:
            round_idx: Current retry round (0-based)
            failure_reason: Why degradation is needed (for logging)

        Returns:
            Dict with degradation parameters
        """
        # Map round to degradation level
        # Round 0: no degradation
        # Round 1: level 1
        # Round 2: level 2
        # Round 3+: level 3
        level_idx = min(round_idx, len(self.levels) - 1)
        config = self.levels[level_idx]

        logger.info(
            "degrade_ladder_applied",
            round=round_idx,
            level=level_idx,
            reason=failure_reason,
            description=config["description"],
            **{k: v for k, v in config.items() if k not in ["level", "description"]}
        )

        return config

    def apply_to_hpo_config(
        self,
        hpo_cfg: Dict[str, Any],
        degrade_cfg: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Apply degradation config to HPO configuration.

        Args:
            hpo_cfg: Original HPO config
            degrade_cfg: Degradation config from apply()

        Returns:
            Modified HPO config
        """
        cfg = hpo_cfg.copy()

        # Limit symbols
        if degrade_cfg.get("symbols_limit") is not None:
            original_symbols = cfg.get("symbols", [])
            if len(original_symbols) > degrade_cfg["symbols_limit"]:
                cfg["symbols"] = original_symbols[:degrade_cfg["symbols_limit"]]
                logger.info(
                    "symbols_degraded",
                    from_count=len(original_symbols),
                    to_count=len(cfg["symbols"]),
                    symbols=cfg["symbols"]
                )

        # Adjust lookback (if using date range)
        if degrade_cfg.get("lookback_days") is not None:
            # This would require recalculating start date
            # For now, just log the intent
            logger.info(
                "lookback_degradation_noted",
                target_days=degrade_cfg["lookback_days"],
                note="Manual date adjustment needed"
            )

        # Adjust folds in fold_generator
        if degrade_cfg.get("folds") is not None:
            fold_gen = cfg.get("fold_generator", {})
            original_folds = fold_gen.get("n_splits", 5)
            if original_folds > degrade_cfg["folds"]:
                fold_gen["n_splits"] = degrade_cfg["folds"]
                cfg["fold_generator"] = fold_gen
                logger.info(
                    "folds_degraded",
                    from_folds=original_folds,
                    to_folds=degrade_cfg["folds"]
                )

        return cfg

    def get_env_overrides(self, degrade_cfg: Dict[str, Any]) -> Dict[str, str]:
        """
        Get environment variable overrides for threading.

        Args:
            degrade_cfg: Degradation config from apply()

        Returns:
            Dict of env var overrides
        """
        n_threads = min(degrade_cfg.get("n_jobs", 1), 4)

        return {
            "OMP_NUM_THREADS": str(n_threads),
            "MKL_NUM_THREADS": str(n_threads),
            "OPENBLAS_NUM_THREADS": str(n_threads),
            "NUMEXPR_NUM_THREADS": str(n_threads),
        }
