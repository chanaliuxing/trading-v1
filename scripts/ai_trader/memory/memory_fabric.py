from __future__ import annotations

"""
Lightweight Memory Fabric v1.

Purpose:
- Provide three-tier memory (STS/MTS/LTS) with simple file-based storage.
- Allow writing new memory entries and promoting/demoting based on heuristic rules.

Storage layout (JSONL):
- runs/memory/sts.jsonl
- runs/memory/mts.jsonl
- runs/memory/lts.jsonl

This is intentionally simple so it can be swapped out for ClickHouse later.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Iterable
try:
    from libs import metrics  # type: ignore
except Exception:
    metrics = None  # type: ignore
try:
    from scripts.ai_trader.memory import vector_store  # type: ignore
except Exception:
    vector_store = None  # type: ignore
import logging
logger = logging.getLogger(__name__)

RUNS_DIR = Path("runs/memory")
STS_FILE = RUNS_DIR / "sts.jsonl"
MTS_FILE = RUNS_DIR / "mts.jsonl"
LTS_FILE = RUNS_DIR / "lts.jsonl"


@dataclass
class MemoryEntry:
    content: Dict[str, Any]
    memory_tier: str  # "STS" | "MTS" | "LTS"
    created_at: str
    last_used_at: Optional[str] = None
    use_count: int = 0
    doc_id: Optional[str] = None
    promotion_rule: str = "use_count>=3"
    decay_rule: str = "age_days>7"  # heuristic for STS demotion/removal
    consolidation_batch: str = "nightly"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def _append(path: Path, entry: MemoryEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")


def write_memory(
    tier: str,
    content: Dict[str, Any],
    promotion_rule: str = "use_count>=3",
    decay_rule: str = "age_days>7",
    doc_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> str:
    now = created_at or datetime.now(timezone.utc).isoformat()
    doc_id = doc_id or f"{tier.lower()}_{now}"
    entry = MemoryEntry(
        content=content,
        memory_tier=tier.upper(),
        created_at=now,
        last_used_at=now,
        use_count=0,
        doc_id=doc_id,
        promotion_rule=promotion_rule,
        decay_rule=decay_rule,
    )
    if entry.memory_tier == "STS":
        _append(STS_FILE, entry)
    elif entry.memory_tier == "MTS":
        _append(MTS_FILE, entry)
    elif entry.memory_tier == "LTS":
        _append(LTS_FILE, entry)
    else:
        raise ValueError("Unknown memory tier")

    # Optional: also write to vector store for semantic search
    if vector_store:
        try:
            vector_store.add(
                doc_id=doc_id,
                text=json.dumps(content, ensure_ascii=False),
                metadata={"tier": entry.memory_tier, "created_at": entry.created_at, "doc_id": doc_id},
            )
        except Exception as e:
            logger.warning(f"Vector store add failed: {e}")
    else:
        logger.debug("vector_store_not_available_skip", doc_id=doc_id)
    return doc_id


def query_similar_memories(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    if not vector_store:
        logger.debug("vector_store_not_available_query_skip")
        return []
    try:
        results = vector_store.search(query, top_k=top_k)
        return results
    except Exception as e:
        logger.warning(f"Vector search failed: {e}")
        return []


def _load(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def consolidate(now: datetime | None = None) -> None:
    """
    Heuristic consolidation:
    - STS: promote to MTS if use_count>=3 or age_days>1
    - MTS: promote to LTS if use_count>=5 and age_days>7
    - STS: drop if age_days>7
    """
    now = now or datetime.now(timezone.utc)
    # Promote from STS to MTS
    sts_rows = _load(STS_FILE)
    kept_sts: List[Dict[str, Any]] = []
    mts_new: List[Dict[str, Any]] = []
    for r in sts_rows:
        created = _parse_dt(r.get("created_at"), now)
        age_days = (now - created).days if created else 0
        use_cnt = int(r.get("use_count", 0) or 0)
        if use_cnt >= 3 or age_days > 1:
            r["memory_tier"] = "MTS"
            mts_new.append(r)
        elif age_days > 7:
            # drop old STS
            continue
        else:
            kept_sts.append(r)
    _write_all(STS_FILE, kept_sts)
    _append_many(MTS_FILE, mts_new)

    # Promote from MTS to LTS
    mts_rows = _load(MTS_FILE)
    kept_mts: List[Dict[str, Any]] = []
    lts_new: List[Dict[str, Any]] = []
    for r in mts_rows:
        created = _parse_dt(r.get("created_at"), now)
        age_days = (now - created).days if created else 0
        use_cnt = int(r.get("use_count", 0) or 0)
        if use_cnt >= 5 and age_days > 7:
            r["memory_tier"] = "LTS"
            lts_new.append(r)
        else:
            kept_mts.append(r)
    _write_all(MTS_FILE, kept_mts)
    _append_many(LTS_FILE, lts_new)


def _parse_dt(s: str | None, default: datetime) -> datetime:
    if not s:
        return default
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return default


def _write_all(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def _append_many(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# New: mark a memory entry as accessed to increment use_count/last_used_at
def mark_accessed(tier: str, entry_index: int) -> None:
    tier = tier.upper()
    if tier == "STS":
        path = STS_FILE
    elif tier == "MTS":
        path = MTS_FILE
    elif tier == "LTS":
        path = LTS_FILE
    else:
        raise ValueError("Unknown memory tier")

    rows = _load(path)
    if entry_index < 0 or entry_index >= len(rows):
        return
    try:
        rows[entry_index]["use_count"] = int(rows[entry_index].get("use_count", 0) or 0) + 1
        rows[entry_index]["last_used_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        return
    _write_all(path, rows)

def mark_accessed_by_doc_id(doc_id: str) -> None:
    """
    Best-effort mark accessed by stored doc_id across tiers.
    """
    if not doc_id:
        return
    for path in (STS_FILE, MTS_FILE, LTS_FILE):
        rows = _load(path)
        updated = False
        for r in rows:
            if r.get("doc_id") == doc_id:
                try:
                    r["use_count"] = int(r["use_count"] or 0) + 1
                    r["last_used_at"] = datetime.now(timezone.utc).isoformat()
                    updated = True
                except Exception:
                    continue
        if updated:
            _write_all(path, rows)


def _tier_path(tier: str) -> Path:
    tier = tier.upper()
    if tier == "STS":
        return STS_FILE
    if tier == "MTS":
        return MTS_FILE
    if tier == "LTS":
        return LTS_FILE
    raise ValueError(f"Unknown tier {tier}")


def load_recent_memories(
    memory_type: str,
    limit: int = 5,
    tiers: Iterable[str] = ("STS", "MTS"),
    required_keys: Optional[Iterable[str]] = None,
    days: Optional[int] = None,
    mark: bool = True,
) -> List[Dict[str, Any]]:
    """
    Fetch recent memory entries by type with light schema validation.
    Skips malformed rows and can mark accessed to drive promotion metrics.
    """
    now = datetime.now(timezone.utc)
    required = set(required_keys or [])
    collected: List[tuple[datetime, str, Dict[str, Any]]] = []
    invalid = 0

    for tier in tiers:
        path = _tier_path(tier)
        rows = _load(path)
        if metrics:
            try:
                available = sum(1 for r in rows if (r.get("content") or {}).get("type") == memory_type)
                metrics.memory_records_available.labels(memory_type=memory_type, tier=tier.upper()).set(available)
            except Exception:
                pass
        for row in rows:
            content = row.get("content") or {}
            if content.get("type") != memory_type:
                continue
            if required and any(k not in content for k in required):
                invalid += 1
                continue
            created = _parse_dt(row.get("created_at"), now)
            if days is not None and (now - created).days > days:
                continue
            collected.append((created, tier, row))

    collected.sort(key=lambda x: x[0], reverse=True)
    selected = collected[:limit]
    results: List[Dict[str, Any]] = []
    for _, tier, row in selected:
        doc_id = row.get("doc_id")
        if mark and doc_id:
            try:
                mark_accessed_by_doc_id(doc_id)
            except Exception:
                pass
        payload = dict(row.get("content") or {})
        payload["_memory_doc_id"] = doc_id
        payload["_memory_tier"] = tier
        payload["_memory_created_at"] = row.get("created_at")
        payload["_memory_use_count"] = row.get("use_count")
        results.append(payload)

    if metrics:
        try:
            if results:
                metrics.memory_fetch_total.labels(memory_type=memory_type, result="hit").inc()
            else:
                metrics.memory_fetch_total.labels(memory_type=memory_type, result="miss").inc()
            if invalid > 0:
                metrics.memory_fetch_total.labels(memory_type=memory_type, result="invalid").inc(invalid)
        except Exception:
            pass

    return results
