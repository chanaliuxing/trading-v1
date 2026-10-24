# memory_store.py
from __future__ import annotations
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json, os, uuid

try:
    import clickhouse_connect
except Exception:
    clickhouse_connect = None

DEFAULT_DB = os.environ.get("CLICKHOUSE_DB", "trading")

DDL_EPISODES = f"""
CREATE TABLE IF NOT EXISTS {DEFAULT_DB}.llm_episodes
(
  ts         DateTime64(3, 'UTC'),
  provider   LowCardinality(String),
  symbol     LowCardinality(String),
  decision   LowCardinality(String),
  quantity   Int32,
  confidence Float64,
  horizon    LowCardinality(String),
  exp_edge_bps Nullable(Float64),
  reasoning  String,
  price      Nullable(Float64),
  realized_pnl Nullable(Float64),
  meta       JSON,
  episode_id UUID
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts)
TTL ts + toIntervalDay(90)
SETTINGS index_granularity=8192
"""

@dataclass
class Episode:
    ts: str
    provider: str
    symbol: str
    decision: str
    quantity: int
    confidence: float
    horizon: Optional[str]
    exp_edge_bps: Optional[float]
    reasoning: Optional[str]
    price: Optional[float]
    realized_pnl: Optional[float]
    meta: Dict[str, Any]
    episode_id: str

class LLMTradingMemory:
    def __init__(self, ch_url: Optional[str] = None):
        self.ch_client = None
        self.local_dir = ".llm_memory"
        os.makedirs(self.local_dir, exist_ok=True)
        if clickhouse_connect and os.environ.get("CLICKHOUSE_HOST"):
            try:
                self.ch_client = clickhouse_connect.get_client(
                    host=os.environ["CLICKHOUSE_HOST"],
                    port=int(os.environ.get("CLICKHOUSE_PORT", 8123)),
                    username=os.environ.get("CLICKHOUSE_USER", "default"),
                    password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
                    database=DEFAULT_DB,
                    connect_timeout=10,
                )
                self.ch_client.command(DDL_EPISODES)
            except Exception as e:
                print(f"[memory] ClickHouse unavailable, fallback to file. {e}")
                self.ch_client = None

    def _local_path(self, symbol: str, provider: str) -> str:
        return os.path.join(self.local_dir, f"{provider}_{symbol}.jsonl")

    def append(self, episode: Episode):
        if self.ch_client:
            self.ch_client.insert(
                table=f"{DEFAULT_DB}.llm_episodes",
                column_names=[
                    "ts","provider","symbol","decision","quantity","confidence",
                    "horizon","exp_edge_bps","reasoning","price","realized_pnl","meta","episode_id"
                ],
                data=[[
                    datetime.fromisoformat(episode.ts.replace("Z","")).astimezone(timezone.utc),
                    episode.provider, episode.symbol, episode.decision,
                    episode.quantity or 0, float(episode.confidence or 0.0),
                    episode.horizon, episode.exp_edge_bps, episode.reasoning or "",
                    episode.price, episode.realized_pnl, json.dumps(episode.meta or {}),
                    uuid.UUID(episode.episode_id)
                ]],
            )
        else:
            with open(self._local_path(episode.symbol, episode.provider), "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(episode), ensure_ascii=False) + "\n")

    def recent(self, symbol: str, provider: str, limit: int = 20) -> List[Dict[str, Any]]:
        if self.ch_client:
            rows = self.ch_client.query(
                f"SELECT ts, decision, quantity, confidence, horizon, exp_edge_bps, price, realized_pnl, meta "
                f"FROM {DEFAULT_DB}.llm_episodes WHERE symbol=%(s)s AND provider=%(p)s "
                f"ORDER BY ts DESC LIMIT %(n)s",
                parameters={"s": symbol, "p": provider, "n": limit},
            ).result_rows or []
            out = []
            for r in rows:
                out.append({
                    "ts": r[0].isoformat(),
                    "decision": r[1],
                    "quantity": r[2],
                    "confidence": r[3],
                    "horizon": r[4],
                    "exp_edge_bps": r[5],
                    "price": r[6],
                    "realized_pnl": r[7],
                    "meta": json.loads(r[8]) if r[8] else {},
                })
            return out
        # local
        path = self._local_path(symbol, provider)
        if not os.path.exists(path):
            return []
        items = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    items.append(json.loads(line))
                except:
                    pass
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return items[:limit]
