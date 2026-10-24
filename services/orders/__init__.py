from .protection import (
    build_protection_key,
    build_protection_metadata,
    compute_price_band_id,
    normalize_qty_bucket,
)


def __getattr__(name: str):
    if name == "UnifiedOrderManager":
        from .manager import UnifiedOrderManager

        return UnifiedOrderManager
    if name == "PositionLedger":
        from .ledger import PositionLedger

        return PositionLedger
    if name == "OrderTruthService":
        from .truth import OrderTruthService

        return OrderTruthService
    raise AttributeError(name)

__all__ = [
    "UnifiedOrderManager",
    "PositionLedger",
    "OrderTruthService",
    "build_protection_key",
    "build_protection_metadata",
    "compute_price_band_id",
    "normalize_qty_bucket",
]
