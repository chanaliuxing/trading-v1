import os, json, time, base64, zlib, threading, datetime, random
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

try:
    import numpy as np  # type: ignore
except Exception:
    np = None  # type: ignore

try:
    import torch  # type: ignore
except Exception:
    torch = None  # type: ignore


def _b64z(obj) -> str:
    try:
        raw = json.dumps(obj).encode("utf-8")
        return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    except Exception:
        return ""


def _unb64z(s: str):
    if not s:
        return None
    try:
        raw = zlib.decompress(base64.b64decode(s.encode("ascii")))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


class ClickHouseClient:
    """Minimal JSONEachRow client for wf_state insert/query via HTTP.

    If requests is unavailable or CH is unreachable, failures are swallowed; local checkpoints still work.
    """

    def __init__(self, url: str = "http://localhost:8123", user: str = "", password: str = "",
                 database: str = "trading", timeout: int = 7):
        self.url = url.rstrip("/")
        self.auth = (user, password) if (user or password) else None
        self.db = database
        self.timeout = timeout
        if requests is None:
            # Degrade to no-op client
            self._disabled = True
        else:
            self._disabled = False

    def query(self, sql: str) -> None:
        if self._disabled:
            return
        try:
            r = requests.post(f"{self.url}/?database={self.db}", data=sql.encode("utf-8"),
                              auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
        except Exception:
            # swallow; local ckpt remains source of truth
            pass

    def insert_wf_state(self, rows: list[dict]):
        if not rows or self._disabled:
            return
        try:
            values = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            sql = "INSERT INTO wf_state FORMAT JSONEachRow\n" + values
            self.query(sql)
        except Exception:
            pass


class CheckpointManager:
    """
    Dual write checkpoints: ClickHouse + local JSON; crash-safe; RNG-capturing.
    Key: (run_id, study, trial_id, step, fold, chunk)
    """

    def __init__(self, run_id: str, study: str,
                 ch: Optional[ClickHouseClient] = None,
                 local_dir: Optional[str] = None,
                 enable_rng: bool = True):
        self.run_id = str(run_id)
        self.study = str(study)
        self.ch = ch
        self.enable_rng = enable_rng
        self.seq = 0

        self.local_dir = Path(local_dir or f"runs/{self.run_id}")
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.local_path = self.local_dir / "checkpoint.json"
        self._lock = threading.Lock()
        self._cache_latest: Dict[str, Any] = {}

        # simple lock file to signal active run
        try:
            (self.local_dir / ".lock").write_text(str(os.getpid()))
        except Exception:
            pass

    def _rng_snapshot(self) -> dict:
        if not self.enable_rng:
            return {}
        snap = {}
        try:
            snap["py"] = _b64z(random.getstate())
        except Exception:
            pass
        try:
            if np is not None:
                snap["np"] = _b64z(np.random.get_state())
        except Exception:
            pass
        try:
            if torch is not None:
                snap["torch"] = _b64z(torch.get_rng_state().tolist())
        except Exception:
            pass
        return snap

    def _rng_restore(self, snap: dict):
        if not snap:
            return
        try:
            if snap.get("py"):
                random.setstate(_unb64z(snap["py"]))
        except Exception:
            pass
        try:
            if np is not None and snap.get("np"):
                np.random.set_state(tuple(_unb64z(snap["np"])))
        except Exception:
            pass
        try:
            if torch is not None and snap.get("torch"):
                state = _unb64z(snap["torch"]) or []
                if state:
                    import torch as _t  # type: ignore
                    _t.set_rng_state(_t.tensor(state, dtype=_t.uint8))
        except Exception:
            pass

    def _key(self, trial_id: int, step: str, fold: int, chunk: int) -> str:
        return f"{self.run_id}:{self.study}:{trial_id}:{step}:{fold}:{chunk}"

    def _load_local(self) -> dict:
        try:
            if self.local_path.exists():
                return json.loads(self.local_path.read_text())
        except Exception:
            pass
        return {"latest": None, "by_key": {}}

    def _write_local(self, obj: dict):
        tmp = self.local_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False))
        tmp.replace(self.local_path)

    def latest(self) -> Optional[Dict[str, Any]]:
        if self._cache_latest:
            return self._cache_latest
        return self._load_local().get("latest")

    def load_for(self, trial_id: int) -> Dict[str, Any]:
        local = self._load_local()
        best = None
        for row in local.get("by_key", {}).values():
            if int(row.get("trial_id", -1)) != int(trial_id):
                continue
            if (best is None) or int(row.get("seq", 0)) > int(best.get("seq", 0)):
                best = row
        if best:
            snap = {k: best.get(k) for k in ("rng_py", "rng_np", "rng_torch")}
            snap2 = {k.split("_", 1)[1]: v for k, v in snap.items() if v}
            self._rng_restore(snap2)
        return best or {}

    def save(self, *, trial_id: int, step: str, fold: int = 0, chunk: int = 0,
             status: str = "in_progress", msg: str = "", payload: Optional[dict] = None):
        with self._lock:
            self.seq += 1
            now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            rng = self._rng_snapshot()
            row = {
                "run_id": self.run_id,
                "study": self.study,
                "trial_id": int(trial_id),
                "step": step,
                "fold": int(fold),
                "chunk": int(chunk),
                "status": status,
                "msg": msg or "",
                "rng_py": rng.get("py", ""),
                "rng_np": rng.get("np", ""),
                "rng_torch": rng.get("torch", ""),
                "payload_json": json.dumps(payload or {}, ensure_ascii=False),
                "updated_at": now,
                "seq": int(self.seq),
            }
            local = self._load_local()
            key = self._key(row["trial_id"], step, row["fold"], row["chunk"])
            local["latest"] = row
            local.setdefault("by_key", {})[key] = row
            self._write_local(local)
            self._cache_latest = row
            try:
                if self.ch:
                    self.ch.insert_wf_state([row])
            except Exception as e:
                print(f"[Checkpoint] WARNING: Failed to write to ClickHouse wf_state: {e}")

    def heartbeat(self, trial_id: int, fold: int = 0, chunk: int = 0, msg: str = ""):
        self.save(trial_id=trial_id, step="heartbeat", fold=fold, chunk=chunk, status="in_progress", msg=msg)

    def mark_done(self, trial_id: int, step: str, fold: int = 0, chunk: int = 0, payload: Optional[dict] = None):
        self.save(trial_id=trial_id, step=step, fold=fold, chunk=chunk, status="done", payload=payload)

    def mark_failed(self, trial_id: int, step: str, fold: int = 0, chunk: int = 0, msg: str = ""):
        self.save(trial_id=trial_id, step=step, fold=fold, chunk=chunk, status="failed", msg=msg)

