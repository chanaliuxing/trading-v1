"""Constants for AI Trader control plane.

Centralizes magic numbers and filenames for better maintainability.
"""

# Memory lookback window for building context to the Brain
MEMORY_LOOKBACK_DAYS = 14  # two weeks is a balanced regime window

# Default RL deploy thresholds (used if config policies missing)
RL_MIN_SHARPE = 1.0        # minimum Sharpe to consider enabling RL
RL_MAX_MDD = 0.20          # maximum acceptable drawdown

# Decision file names for morning gating persistence
DECISION_FILES = {
    "health": "morning_decision_health.json",
    "data": "morning_decision_data.json",
    "generic": "morning_decision.json",
}

