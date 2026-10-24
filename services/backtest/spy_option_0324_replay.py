"""Backward-compatible wrapper for the isolated option replay implementation.

The implementation now lives under `services.option_replay.core`.
This module remains so older scripts/tests/imports keep working during the
Phase A migration.
"""

from services.option_replay.core import *  # noqa: F401,F403
