import asyncio
import json
import os
from libs.db.postgres import PostgresDB

def _safe_json_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

def _protection_detail(row):
    order_type = str(row.get("order_type") or "").strip().upper().replace(" ", "_")
    order_ref = str(row.get("order_ref") or "").strip().upper()
    if order_type not in {"STP", "STP_LMT", "TRAIL", "TRAIL_LIMIT"} and ":TRAIL" not in order_ref and ":STOP" not in order_ref:
        return {}
    note = _safe_json_obj(row.get("note"))
    extras = note.get("extras") if isinstance(note.get("extras"), dict) else {}
    style = note.get("order_style") if isinstance(note.get("order_style"), dict) else {}
    return {
        "kind": order_type or str(style.get("kind") or "").strip().upper().replace(" ", "_"),
        "stop_px": style.get("stop_px") or extras.get("stop_px") or row.get("stop_px"),
        "trail_amount": style.get("trail_amount") or extras.get("trail_amount"),
        "trail_percent": style.get("trail_percent") or extras.get("trail_percent"),
        "trail_stop_px": style.get("trail_stop_px") or extras.get("trail_stop_px"),
        "lmt_price_offset": style.get("lmt_price_offset") or extras.get("lmt_price_offset"),
        "protection_spec_mode": extras.get("protection_spec_mode") or note.get("protection_spec_mode"),
        "protection_position_id": extras.get("protection_position_id") or note.get("protection_position_id"),
    }

async def inspect_orders():
    pg = PostgresDB()
    # Using correct column names from schema
    query = """
        SELECT broker_order_id, order_ref, symbol, side, order_type, qty, limit_px, stop_px, state, note, created_at
        FROM core.core_order 
        WHERE broker_order_id IN ('6', '8');
    """
    rows = await pg.fetchall(query)
    for row in rows:
        payload = dict(row)
        payload["protection"] = _protection_detail(payload)
        print(json.dumps(payload, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(inspect_orders())
