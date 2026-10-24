"""
Portfolio Service
Position tracking, PnL accounting, and NAV calculation
"""
from .ledger import PnLLedger, Lot, FillResult

try:
    from .service import PortfolioService, PositionSnapshot, StrategySummary
except ModuleNotFoundError:  # pragma: no cover
    # Optional dependency path (e.g., asyncpg) not installed in minimal test envs.
    PortfolioService = None  # type: ignore
    PositionSnapshot = None  # type: ignore
    StrategySummary = None  # type: ignore

__all__ = [
    'PnLLedger',
    'Lot',
    'FillResult',
    'PortfolioService',
    'PositionSnapshot',
    'StrategySummary',
]
