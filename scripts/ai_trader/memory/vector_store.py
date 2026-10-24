from __future__ import annotations

"""
Lightweight VectorStore accessor for memory_fabric.
Wraps libs.memory.vector_store with a singleton and simple add/query helpers.
"""

import logging
from typing import List, Dict, Any

try:
    from libs.memory.vector_store import VectorStore as _VS  # type: ignore
except Exception:
    _VS = None  # type: ignore

logger = logging.getLogger(__name__)
_store = None


def get_store():
    global _store
    if _store is None and _VS:
        try:
            _store = _VS()
        except Exception as e:
            logger.warning(f"vector_store_init_failed: {e}")
            _store = None
    return _store


def add(doc_id: str, text: str, metadata: Dict[str, Any]) -> None:
    store = get_store()
    if not store:
        return
    try:
        store.add(doc_id, text, metadata)
    except Exception as e:
        logger.warning(f"vector_store_add_failed: {e}")


def search(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    store = get_store()
    if not store:
        return []
    try:
        res = store.search(query, top_k=top_k)
        return [{"id": rid, "text": text, "score": score} for rid, text, score in res]
    except Exception as e:
        logger.warning(f"vector_store_search_failed: {e}")
        return []
