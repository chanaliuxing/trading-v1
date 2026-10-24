from __future__ import annotations

import threading
from pathlib import Path


_LOCK = threading.Lock()
_PATH_OWNERS: dict[str, str] = {}


def claim_paths(*, strategy_id: str, paths: list[Path]) -> None:
    normalized = [str(Path(p).expanduser().resolve()) for p in paths]
    with _LOCK:
        for path in normalized:
            owner = _PATH_OWNERS.get(path)
            if owner is not None and owner != strategy_id:
                raise RuntimeError(
                    f"runtime_path_conflict path={path} owner={owner} contender={strategy_id}"
                )
        for path in normalized:
            _PATH_OWNERS[path] = strategy_id


def release_paths(*, strategy_id: str, paths: list[Path]) -> None:
    normalized = [str(Path(p).expanduser().resolve()) for p in paths]
    with _LOCK:
        for path in normalized:
            if _PATH_OWNERS.get(path) == strategy_id:
                _PATH_OWNERS.pop(path, None)
