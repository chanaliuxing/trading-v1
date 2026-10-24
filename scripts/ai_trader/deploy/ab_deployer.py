import json
from pathlib import Path


class ABDeployer:
    def __init__(self, registry_path: str, promote_rules: dict, default_w: dict):
        self.path = Path(registry_path)
        self.rules = promote_rules or {"sharpe_gain_ratio": 1.05, "mdd_improve_ratio": 0.90}
        self.default_w = default_w or {"champion": 0.7, "challenger": 0.3}

    def propose(self, champion: dict, challenger: dict) -> dict:
        try:
            ok = (
                float(challenger.get("sharpe", 0)) >= float(champion.get("sharpe", 0)) * self.rules["sharpe_gain_ratio"]
                and float(challenger.get("mdd", 1)) <= float(champion.get("mdd", 1)) * self.rules["mdd_improve_ratio"]
            )
        except Exception:
            ok = False
        if ok:
            plan = dict(self.default_w)
        else:
            plan = {"champion": 1.0, "challenger": 0.0}
        return {"weights": plan, "reason": "auto_ab"}

    def save_registry(self, cc_json: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cc_json, indent=2), encoding="utf-8")

