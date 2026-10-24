"""
Execution service
"""
try:
    from .execution_service import ExecutionService, ExecutionResult
    from .safety_kernel import SafetyKernel
    from .fill_event_handler import FillEventHandler
except (ModuleNotFoundError, ImportError):  # pragma: no cover
    # Optional dependency path (e.g., asyncpg) not installed in minimal test envs,
    # or partial-import/circular-import during lightweight state-model usage.
    ExecutionService = None  # type: ignore
    ExecutionResult = None  # type: ignore
    SafetyKernel = None  # type: ignore
    FillEventHandler = None  # type: ignore
from .models import (
    Fill,
    IntentFlags,
    OCOPair,
    OrderIntent,
    OrderRole,
    OrderSide,
    OrderState,
    OrderType,
    OwnerKey,
    PlacedOrder,
    QuarantineScope,
    quarantine_matches_intent,
)

__all__ = [
    'ExecutionService',
    'ExecutionResult',
    'SafetyKernel',
    'FillEventHandler',
    'PlacedOrder',
    'OrderIntent',
    'OwnerKey',
    'OrderRole',
    'IntentFlags',
    'QuarantineScope',
    'quarantine_matches_intent',
    'Fill',
    'OCOPair',
    'OrderState',
    'OrderSide',
    'OrderType',
]
