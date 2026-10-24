import os
import subprocess
import shlex
from pathlib import Path


class ExecGate:
    """Checks IBKR/TWS connectivity via temp/check_ibkr_status.py if available.

    project_root can be passed explicitly, via env TRADING_PROJECT_ROOT, or inferred
    from this file location to improve portability.
    """

    def __init__(self, project_root: str | None = None):
        # Prefer explicit arg, then env, finally infer repo root
        inferred = str(Path(__file__).resolve().parents[3])
        self.project_root = project_root or os.getenv("TRADING_PROJECT_ROOT", inferred)

    def check(self) -> dict:
        try:
            cmd = f"python {self.project_root}/temp/check_ibkr_status.py"
            out = subprocess.check_output(shlex.split(cmd), text=True, stderr=subprocess.STDOUT, timeout=30)
            ok = ("Connected" in out) or ("IBKR Connected: True" in out) or ("Connected: True" in out)
            return {"ok": bool(ok), "sample": out[:200]}
        except Exception as e:
            return {"ok": False, "reasons": [f"ibkr_check_failed:{e}"]}
