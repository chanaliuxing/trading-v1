from __future__ import annotations
from typing import Dict, Any, Optional, Iterable
import os

_MLF = None
_enabled = False
_initialized = False


def init_mlflow(tracking_uri: Optional[str] = None, experiment: Optional[str] = None, enabled: Optional[bool] = None) -> None:
    global _MLF, _enabled, _initialized
    if enabled is not None:
        _enabled = bool(enabled)
    else:
        _enabled = bool(int(os.getenv("MLFLOW_ENABLED", "1")))
    if not _enabled:
        _initialized = True
        return
    try:
        import mlflow
    except Exception:
        _enabled = False
        _initialized = True
        return
    _MLF = mlflow
    _MLF.set_tracking_uri(tracking_uri or os.getenv("MLFLOW_TRACKING_URI", "file:./mlruns"))
    exp_name = experiment or os.getenv("MLFLOW_EXPERIMENT", "AI Trader Evolution")
    _MLF.set_experiment(exp_name)
    _initialized = True


def _ok() -> bool:
    return _initialized and _enabled and _MLF is not None


def start_run(run_name: Optional[str] = None, tags: Optional[Dict[str, Any]] = None):
    if not _ok():
        return None
    return _MLF.start_run(run_name=run_name, tags=tags)


def end_run():
    if not _ok():
        return
    try:
        _MLF.end_run()
    except Exception:
        pass


def log_metrics(metrics: Dict[str, float]) -> None:
    if not _ok():
        return
    try:
        _MLF.log_metrics({k: float(v) for k, v in (metrics or {}).items() if isinstance(v, (int, float))})
    except Exception:
        pass


def log_params(params: Dict[str, Any]) -> None:
    if not _ok():
        return
    flat = {}
    for k, v in (params or {}).items():
        if isinstance(v, (str, int, float, bool)):
            flat[k] = v
        else:
            try:
                flat[k] = str(v)
            except Exception:
                continue
    try:
        _MLF.log_params(flat)
    except Exception:
        pass


def log_artifacts(paths: Iterable[str]) -> None:
    if not _ok():
        return
    for p in (paths or []):
        try:
            if p and os.path.exists(p):
                _MLF.log_artifact(p)
        except Exception:
            continue

