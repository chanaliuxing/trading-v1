from __future__ import annotations

"""
Helper to fetch similar memories for Research/Brain prompts and mark them as accessed.
"""

from typing import List, Dict, Any

try:
    from scripts.ai_trader.memory.memory_fabric import query_similar_memories, mark_accessed_by_doc_id  # type: ignore
except Exception:
    query_similar_memories = None  # type: ignore
    mark_accessed_by_doc_id = None  # type: ignore


def fetch_relevant_memories(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Fetch semantic memories and mark them as accessed to drive promotion logic.
    Note: memory_fabric stores doc_id in metadata; mark_accessed_by_doc_id is used when available.
    """
    if not query_similar_memories:
        return []
    try:
        results = query_similar_memories(query, top_k=top_k)
        # mark accessed best-effort by doc_id
        if mark_accessed_by_doc_id:
            for res in results:
                # prefer explicit doc_id in metadata, fallback to vector id
                meta = res.get("metadata") or {}
                doc_id = meta.get("doc_id") or res.get("id")
                if doc_id:
                    mark_accessed_by_doc_id(doc_id)
        return results
    except Exception:
        return []
