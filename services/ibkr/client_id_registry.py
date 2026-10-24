from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)


class ClientIdRegistry:
    """Deterministic role -> client_id mapping with persisted runtime state."""

    def __init__(self) -> None:
        self._cfg = config.get("ib.registry", {}) or {}
        self._state_path = Path(str(self._cfg.get("state_file", "runtime/state/ib_client_ids.json")))
        self._state: Dict[str, int] = self._load_state()

    def _load_state(self) -> Dict[str, int]:
        try:
            if not self._state_path.exists():
                return {}
            raw = self._state_path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            out: Dict[str, int] = {}
            for k, v in data.items():
                try:
                    out[str(k)] = int(v)
                except Exception:
                    continue
            return out
        except Exception as exc:
            logger.warning("ib_client_id_registry_load_failed", file=str(self._state_path), error=str(exc))
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception as exc:
            logger.warning("ib_client_id_registry_save_failed", file=str(self._state_path), error=str(exc))

    @staticmethod
    def _normalize_role(role: str) -> str:
        return str(role or "").strip().lower()

    def _default_for_role(self, role: str) -> int:
        ib_cfg = config.ib_config
        base = int(ib_cfg.get("client_id", 7))
        role_n = self._normalize_role(role)
        cfg = self._cfg
        if role_n == "master":
            return int(cfg.get("master_client_id", 0))
        if role_n == "market":
            return int(cfg.get("market_client_id", base + 20))
        if role_n == "exec":
            return int(cfg.get("exec_client_id", base + 200))
        if role_n == "vix":
            return int(cfg.get("vix_client_id", base + 102))
        if role_n == "backfill":
            return int(cfg.get("backfill_client_id", base + 40))
        if role_n == "options_quotes":
            return int(cfg.get("options_quotes_client_id", base + 70))
        return int(base + 500)

    def resolve(self, role: str) -> int:
        key = self._normalize_role(role)
        if key in self._state:
            return int(self._state[key])
        resolved = self._default_for_role(key)
        self._state[key] = int(resolved)
        self._save_state()
        logger.info("ib_client_id_registry_resolved", role=key, client_id=int(resolved))
        return int(resolved)

    def get_state_snapshot(self) -> Dict[str, int]:
        return dict(self._state)


_REGISTRY = ClientIdRegistry()


def get_client_id_registry() -> ClientIdRegistry:
    return _REGISTRY

