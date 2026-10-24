import json
from pathlib import Path
import datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def write_action_plan(path: str, payload: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload or {})
    payload["generated_at"] = datetime.datetime.now(NY_TZ).isoformat()
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

