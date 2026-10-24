"""Real-time inference manager."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from contextlib import nullcontext
from typing import Any, Deque, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from models import DecisionScheme, LSTMModel, ModelAdapter, XGBModel
from obs.audit import REASONS, log_event

try:  # Optional tracing
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None  # type: ignore

_TRACER = trace.get_tracer("trader.inference") if trace else None


class InferenceManager:
    """Hot-loads trained models and produces ``ml_signal`` fields for strategies."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = cfg or {}
        self.active_path = Path(self.cfg.get("active_path", "models/active"))
        self.scheme = DecisionScheme.from_dict(self.cfg.get("decision"))
        self.fallback_zero = bool(self.cfg.get("fallback_zero", True))
        self.model: Optional[ModelAdapter] = None
        self.meta: Dict[str, Any] = {}
        self._last_mtime = 0.0
        self._buffers: Dict[str, Deque[Dict[str, Any]]] = {}
        self._load_if_needed(force=True)

    # ------------------------------------------------------------------
    def _load_if_needed(self, force: bool = False) -> None:
        meta_path = self.active_path / "meta.json"
        if not meta_path.exists():
            if force:
                self.model = None
            return
        mtime = meta_path.stat().st_mtime
        if not force and mtime <= self._last_mtime:
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        model_type = meta.get("model_type") or meta.get("type")
        model_cls: Optional[type[ModelAdapter]] = None
        if model_type in {"XGBModel", "xgb", "xgboost"}:
            model_cls = XGBModel
        elif model_type in {"LSTMModel", "lstm"}:
            model_cls = LSTMModel
        else:
            self.model = None
            self.meta = {}
            self._last_mtime = mtime
            return

        decision_scheme = DecisionScheme.from_dict(meta.get("decision_scheme"))
        model = model_cls(task=meta.get("task", "reg"), hparams=meta.get("hparams"), decision_scheme=decision_scheme)
        model.load(str(self.active_path))
        self.model = model
        self.meta = meta
        self._last_mtime = mtime
        self._buffers.clear()

    # ------------------------------------------------------------------
    def _update_buffer(self, symbol: str, row: Dict[str, Any]) -> Optional[pd.DataFrame]:
        if not self.model:
            return None
        lookback = getattr(self.model, "lookback", 1)
        buf = self._buffers.setdefault(symbol, deque(maxlen=max(lookback, 1)))
        buf.append(dict(row))
        if len(buf) < max(lookback, 1):
            return None
        df = pd.DataFrame(list(buf))
        return df

    # ------------------------------------------------------------------
    def infer_row(self, row: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        self._load_if_needed()
        out = dict(row)
        diag = {"infer_ms": 0.0, "error": 0}

        if self.model is None:
            if self.fallback_zero:
                out.setdefault("ml_signal", 0.0)
                out.setdefault("ml_pred", 0.0)
                out.setdefault("ml_conf", 0.0)
                log_event(
                    "inference_fallback",
                    component="InferenceManager",
                    symbol=row.get("symbol"),
                    reason="fallback_zero",
                    reason_detail=REASONS["fallback_zero"],
                    msg="no_active_model",
                )
            return out, diag

        symbol = str(row.get("symbol", ""))
        df = pd.DataFrame([row])

        if isinstance(self.model, LSTMModel):
            df_buffer = self._update_buffer(symbol, row)
            if df_buffer is None:
                out.setdefault("ml_signal", 0.0 if self.fallback_zero else np.nan)
                out.setdefault("ml_pred", 0.0 if self.fallback_zero else np.nan)
                out.setdefault("ml_conf", 0.0 if self.fallback_zero else np.nan)
                if self.fallback_zero:
                    log_event(
                        "inference_fallback",
                        component="InferenceManager",
                        symbol=row.get("symbol"),
                        reason="fallback_zero",
                        reason_detail=REASONS["fallback_zero"],
                        msg="buffer_warmup",
                    )
                return out, diag
            df = df_buffer

        span_cm = _TRACER.start_as_current_span("inference") if _TRACER else nullcontext()
        with span_cm as span:  # type: ignore[attr-defined]
            t0 = time.perf_counter()
            try:
                preds = self.model.predict(df)
            except Exception as exc:  # pragma: no cover - runtime guard
                diag["error"] = 1
                if self.fallback_zero:
                    out.setdefault("ml_signal", 0.0)
                    out.setdefault("ml_pred", 0.0)
                    out.setdefault("ml_conf", 0.0)
                diag["message"] = str(exc)
                if span_cm is not None and hasattr(span, "record_exception"):
                    span.record_exception(exc)
                log_event(
                    "inference_error",
                    level="ERROR",
                    component="InferenceManager",
                    symbol=row.get("symbol"),
                    reason="fallback_zero" if self.fallback_zero else "error",
                    reason_detail=REASONS.get("fallback_zero", "fallback") if self.fallback_zero else str(exc),
                    error=str(exc),
                )
                return out, diag

        infer_ms = (time.perf_counter() - t0) * 1e3
        diag["infer_ms"] = float(infer_ms)
        pred = float(preds[-1]) if preds.size else 0.0
        out["ml_pred"] = pred

        if self.model.task == "clf":
            conf = float(min(1.0, abs(pred - 0.5) * 2))
        else:
            conf = float(min(1.0, abs(pred)))
        out["ml_conf"] = conf

        sig = float(self.model.to_signal(np.array([pred]))[-1])
        out["ml_signal"] = sig

        if span_cm is not None and hasattr(span, "set_attribute"):
            span.set_attribute("symbol", str(row.get("symbol")))
            span.set_attribute("pred", pred)
            span.set_attribute("signal", sig)
            span.set_attribute("conf", conf)
            span.set_attribute("infer_ms", float(infer_ms))

        log_event(
            "inference",
            component="InferenceManager",
            symbol=row.get("symbol"),
            model=self.meta.get("model_type"),
            infer_ms=float(infer_ms),
            pred=pred,
            signal=sig,
            conf=conf,
            error=0,
        )

        return out, diag
