#!/usr/bin/env python3
from __future__ import annotations

"""
AI Brain ML Gateway

Thin decision layer that sits on top of ModelMLInferenceService:
- Uses model metadata (metadata.json) to derive thresholds.
- Uses OutlierDetector + regime filters as a safety net.
- Returns a clean decision envelope for strategies / AI Brain.
"""

from typing import Dict, Any

try:
    from libs.logger import get_logger
except Exception:  # pragma: no cover - fallback
    import logging

    def get_logger(name: str):
        logging.basicConfig(level=logging.INFO)
        return logging.getLogger(name)

from pathlib import Path
from datetime import date, datetime, timezone
import json

from services.ml.model_inference_service import ModelMLInferenceService
from services.ml.outlier_detector import OutlierDetector


class AiBrainMLGateway:
    """
    AI Brain ML Gateway (Decision Maker).

    - Delegates raw prediction to ModelMLInferenceService.
    - Applies outlier detection + simple regime filter.
    - Translates raw probabilities into trade/hold/blocked decisions.
    """

    def __init__(self, inference_service: ModelMLInferenceService):
        self.service = inference_service
        self.log = get_logger("ml.gateway")

        self.outlier_detector: OutlierDetector | None = None
        if getattr(self.service, "is_ready", False) and getattr(self.service, "scaler", None) is not None:
            try:
                self.outlier_detector = OutlierDetector(self.service.scaler, self.service.feature_cols)
            except Exception as exc:
                self.log.warning("gateway_outlier_init_failed", error=str(exc))

        self._reload_dynamic_config()

    # --- Dynamic config from metadata ----------------------------------------------------

    def _reload_dynamic_config(self) -> None:
        """Load thresholds from model metadata (if present)."""
        meta: Dict[str, Any] = getattr(self.service, "metadata", {}) or {}
        opt = meta.get("optimized_thresholds") or {}

        # Signal thresholds (probability space)
        self.signal_high = float(opt.get("signal_threshold_high", 0.55))
        self.signal_low = float(opt.get("signal_threshold_low", 0.45))

        # Regime / volatility filter based on ATR
        self.baseline_atr = float(meta.get("baseline_atr_14", 0.0) or 0.0)
        self.regime_max_atr_mult = float(opt.get("regime_max_atr_mult", 3.0))

        # Outlier detection config
        drift_cfg = opt.get("drift_config") or {}
        self.z_threshold = float(drift_cfg.get("z_threshold", 10.0))
        self.outlier_ratio = float(drift_cfg.get("outlier_ratio_threshold", 0.3))

        self.log.info(
            "gateway_config_updated",
            signal_cfg=(self.signal_low, self.signal_high),
            regime_cfg=f"{self.regime_max_atr_mult}x_ATR",
            outlier_cfg=drift_cfg,
        )

    # --- Public API ----------------------------------------------------------------------

    def get_decision(self, symbol: str, features: Dict[str, Any]) -> Dict[str, Any]:
        """
        Core decision flow:
            1) ModelMLInferenceService.predict_raw
            2) OutlierDetector.check
            3) Regime filter (ATR-based)
            4) Convert raw_pred into signal/confidence
        """
        # 1) Raw inference
        res = self.service.predict_raw(features)
        if res.get("status") != "ok":
            err = str(res.get("error_reason") or "unknown")
            # If features are structurally invalid/missing, we treat this as a
            # degraded HOLD decision rather than a hard gateway block. This
            # prevents ML Brain from thrashing on perpetual data_integrity
            # blocks when only a subset of live features is available.
            if err.startswith("invalid_features:"):
                self.log.warning(
                    "gateway_features_invalid",
                    symbol=symbol,
                    error_reason=err,
                )
                return {
                    "decision": "hold",
                    "ml_signal": 0.0,
                    "ml_conf": 0.0,
                    "raw_pred": 0.5,
                    "drift_score": 0.0,
                    "status": "degraded",
                    "error_reason": err,
                }
            # Other service-level errors (model not ready, crashes) are still
            # treated as data_integrity blocks.
            return self._block(symbol, "data_integrity", err)

        raw_pred: float = float(res.get("raw_pred", 0.5) or 0.5)
        raw_vec = res.get("raw_vector")

        # 2) Outlier / drift check
        drift_score = 0.0
        if self.outlier_detector is not None and raw_vec is not None:
            try:
                od = self.outlier_detector.check(
                    raw_vec,
                    z_threshold=self.z_threshold,
                    ratio_limit=self.outlier_ratio,
                )
                drift_score = float(od.get("outlier_score", 0.0) or 0.0)
                if bool(od.get("is_outlier")):
                    details = ", ".join(od.get("details", []))
                    return self._block(
                        symbol,
                        "outlier_drift",
                        f"features_deviated: {details}",
                        drift_score=drift_score,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                self.log.warning("outlier_check_failed", error=str(exc))

        # 3) Regime filter (ATR spike vs baseline)
        current_atr = features.get("atr_14")
        if self.baseline_atr > 0.0 and isinstance(current_atr, (int, float)):
            try:
                cur = float(current_atr)
                if cur > self.baseline_atr * self.regime_max_atr_mult:
                    msg = f"volatility_spike: {cur:.4f} > {self.baseline_atr * self.regime_max_atr_mult:.4f}"
                    return self._block(symbol, "regime_filter", msg, drift_score=drift_score)
            except Exception:
                # If ATR is not usable, skip regime filter
                pass

        # 4) Map raw_pred to signal / confidence
        ml_signal = 0.0
        if raw_pred > self.signal_high:
            ml_signal = 1.0
        elif raw_pred < self.signal_low:
            ml_signal = -1.0

        ml_conf = float(abs(raw_pred - 0.5) * 2.0) if ml_signal != 0.0 else 0.0

        return {
            "decision": "trade" if ml_signal != 0.0 else "hold",
            "ml_signal": ml_signal,
            "ml_conf": ml_conf,
            "raw_pred": raw_pred,
            "drift_score": drift_score,
            "status": "ok",
        }

    # --- Internal helpers ----------------------------------------------------------------

    def _block(self, symbol: str, code: str, details: str, **extra: Any) -> Dict[str, Any]:
        self.log.warning("gateway_blocked", symbol=symbol, code=code, details=details)
        payload: Dict[str, Any] = {
            "decision": "blocked",
            "block_reason": f"[{code}] {details}",
            "ml_signal": 0.0,
            "ml_conf": 0.0,
            "raw_pred": 0.5,
            "status": "blocked",
        }
        payload.update(extra)
        # Fire-and-forget append to daily gateway decisions log so ML Brain can
        # later reason about why trades were blocked (data issues vs drift vs
        # extreme regimes). Failures here are non-fatal.
        try:
            self._append_decision_log(symbol, code, payload)
        except Exception:
            pass
        return payload

    def _append_decision_log(self, symbol: str, code: str, payload: Dict[str, Any]) -> None:
        """Append a single gateway decision to runs/ai_trader/<date>/ml_gateway_decisions.jsonl."""
        try:
            root = Path(__file__).resolve().parent.parent.parent
            dstr = date.today().isoformat()
            out_dir = root / "runs" / "ai_trader" / dstr
            out_dir.mkdir(parents=True, exist_ok=True)
            fp = out_dir / "ml_gateway_decisions.jsonl"
             # Attach a concrete timestamp so downstream aggregators can apply
            # recency filters. If the caller did not provide one, use "now".
            ts_val = payload.get("ts")
            if not ts_val:
                ts_val = datetime.now(timezone.utc).isoformat()
            record = {
                "symbol": symbol,
                "code": code,
                "ts": ts_val,
                "decision": payload.get("decision"),
                "status": payload.get("status"),
                "raw_pred": payload.get("raw_pred"),
                "drift_score": payload.get("drift_score"),
                "block_reason": payload.get("block_reason"),
            }
            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Best-effort; we don't want logging failures to affect trading.
            pass


__all__ = ["AiBrainMLGateway"]
