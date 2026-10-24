#!/usr/bin/env python3
"""
Vector Store for Semantic Memory (Meta v3.5 - BGE-M3 Upgrade)

Provides semantic search capabilities using SOTA embeddings.
Supports both Chroma (persistent, production) and FAISS (in-memory, fast).

Upgraded to use EmbeddingService abstraction with BGE-M3 (BAAI/bge-m3):
- SOTA multilingual embeddings (100+ languages)
- Long context support (up to 8192 tokens)
- Free, local, private (no API costs)

Usage:
    from libs.memory.vector_store import VectorStore

    store = VectorStore(backend="chroma")  # Uses BGE-M3 by default

    # Add memories
    store.add("exp_001", "Data quality issues detected, sync triggered", {"date": "2025-12-01"})

    # Semantic search
    results = store.search("我们的数据不太准确", top_k=5)
    # Returns: [("exp_001", "Data quality issues detected...", 0.85), ...]
"""
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class VectorStore:
    """
    Vector store wrapper supporting multiple backends.

    Backends:
    - chroma: Persistent vector database (production)
    - faiss: In-memory vector index (fast, for testing)

    v3.5 Upgrade: Uses EmbeddingService abstraction with BGE-M3 by default
    """

    def __init__(
        self,
        backend: str = "chroma",
        persist_dir: Optional[Path] = None,
        embedding_service: Optional[Any] = None,
        # Deprecated: kept for backward compatibility
        embedding_model: Optional[str] = None
    ):
        """
        Initialize vector store.

        Args:
            backend: "chroma" or "faiss"
            persist_dir: Directory for persistent storage (Chroma only)
            embedding_service: Optional EmbeddingService instance (uses BGE-M3 from config if None)
            embedding_model: DEPRECATED - use embedding_service instead
        """
        self.backend = backend.lower()
        self.persist_dir = persist_dir or Path("runs/memory/vector_store")

        # v3.5: Use EmbeddingService abstraction
        if embedding_service is None:
            try:
                from libs.memory.embedding_service import get_embedding_service_from_config
                self.embedding_service = get_embedding_service_from_config()
                logger.info("✅ VectorStore using BGE-M3 embeddings from config")
            except Exception as e:
                logger.warning(f"Failed to load embedding service from config: {e}")
                logger.info("Falling back to local BGE-M3")
                from libs.memory.embedding_service import LocalBGEEmbedding
                self.embedding_service = LocalBGEEmbedding()
        else:
            self.embedding_service = embedding_service

        self._client = None
        self._collection = None
        self._faiss_index = None
        self._faiss_metadata = []
        self._memory_store: List[Dict[str, Any]] = []

        self._init_backend()

    def _init_backend(self):
        """Initialize the selected backend; degrade to memory-only if deps missing."""
        if self.backend == "chroma":
            try:
                self._init_chroma()
                return
            except Exception as e:
                logger.warning(f"Chroma init failed ({e}); falling back to memory backend")
                self.backend = "memory"
        if self.backend == "faiss":
            try:
                self._init_faiss()
                return
            except Exception as e:
                logger.warning(f"FAISS init failed ({e}); falling back to memory backend")
                self.backend = "memory"

        if self.backend == "memory":
            self._init_memory()
        else:
            raise ValueError(f"Unsupported backend: {self.backend}")

    def _init_chroma(self):
        """Initialize ChromaDB backend with custom embedding function"""
        try:
            import chromadb
            from chromadb.config import Settings
            from chromadb.utils.embedding_functions import EmbeddingFunction

            self.persist_dir.mkdir(parents=True, exist_ok=True)

            # Custom embedding function wrapper for ChromaDB
            class CustomEmbeddingFunction(EmbeddingFunction):
                def __init__(self, embedding_service):
                    self.embedding_service = embedding_service

                def __call__(self, input: List[str]) -> List[List[float]]:
                    """ChromaDB calls this to generate embeddings"""
                    return self.embedding_service.embed_documents(input)

            self._client = chromadb.PersistentClient(
                path=str(self.persist_dir),
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )

            # Get or create collection with custom embedding function
            self._collection = self._client.get_or_create_collection(
                name="memory_fabric_v35",  # New collection name for v3.5 upgrade
                metadata={
                    "description": "Semantic memory with BGE-M3 embeddings",
                    "embedding_model": "BAAI/bge-m3",
                    "version": "3.5"
                },
                embedding_function=CustomEmbeddingFunction(self.embedding_service)
            )

            logger.info(f"ChromaDB initialized at {self.persist_dir}")
            logger.info(f"Collection: memory_fabric_v35 (BGE-M3)")
            logger.info(f"Collection size: {self._collection.count()} documents")

        except ImportError:
            logger.error("ChromaDB not installed. Install: pip install chromadb")
            raise

    def _init_faiss(self):
        """Initialize FAISS backend (uses BGE-M3 via EmbeddingService)"""
        try:
            import faiss

            # FAISS index will be created on first add
            self._faiss_index = None
            self._faiss_metadata = []

            logger.info("FAISS backend initialized (in-memory, BGE-M3)")

        except ImportError:
            logger.error("FAISS not installed. Install: pip install faiss-cpu")
            raise

    def _init_memory(self):
        """Initialize memory-only backend (no vector DB dependencies)."""
        self._memory_store = []
        logger.warning("VectorStore running in memory-only fallback (no Chroma/FAISS)")

    def add(
        self,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Add a document to the vector store.

        Args:
            doc_id: Unique document ID
            text: Text content to embed
            metadata: Optional metadata dict
        """
        metadata = metadata or {}

        if self.backend == "chroma":
            self._add_chroma(doc_id, text, metadata)
        elif self.backend == "faiss":
            self._add_faiss(doc_id, text, metadata)
        elif self.backend == "memory":
            self._add_memory(doc_id, text, metadata)

    def _add_chroma(self, doc_id: str, text: str, metadata: Dict[str, Any]):
        """Add to ChromaDB"""
        try:
            # ChromaDB handles embedding automatically
            self._collection.add(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata]
            )
            logger.debug(f"Added to Chroma: {doc_id}")
        except Exception as e:
            logger.error(f"Failed to add to Chroma: {e}")
            raise

    def _add_faiss(self, doc_id: str, text: str, metadata: Dict[str, Any]):
        """Add to FAISS (uses EmbeddingService with BGE-M3)"""
        import faiss
        import numpy as np

        try:
            # Get embedding using EmbeddingService
            embedding = self.embedding_service.embed_query(text)

            # Initialize index on first add
            if self._faiss_index is None:
                dimension = len(embedding)
                self._faiss_index = faiss.IndexFlatIP(dimension)  # Inner product (cosine similarity)
                logger.info(f"FAISS index created with dimension {dimension} (BGE-M3)")

            # BGE-M3 already returns L2-normalized vectors, but ensure it
            embedding_array = np.array([embedding], dtype=np.float32)
            norm = np.linalg.norm(embedding_array)
            if norm > 0:
                embedding_array = embedding_array / norm

            # Add to index
            self._faiss_index.add(embedding_array)

            # Store metadata
            self._faiss_metadata.append({
                "id": doc_id,
                "text": text,
                "metadata": metadata
            })

            logger.debug(f"Added to FAISS: {doc_id}")

        except Exception as e:
            logger.error(f"Failed to add to FAISS: {e}")
            raise

    def _add_memory(self, doc_id: str, text: str, metadata: Dict[str, Any]):
        """Memory-only add: store embedding if available; still succeed without deps."""
        embedding = None
        try:
            embedding = self.embedding_service.embed_query(text)
        except Exception as e:
            logger.warning(f"Memory backend could not embed text; storing without embedding: {e}")
        self._memory_store.append(
            {"id": doc_id, "text": text, "metadata": metadata, "embedding": embedding}
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, str, float]]:
        """
        Semantic search for similar documents.

        Args:
            query: Query text
            top_k: Number of results to return
            filter_metadata: Optional metadata filter (Chroma only)

        Returns:
            List of (doc_id, text, similarity_score) tuples
        """
        if self.backend == "chroma":
            return self._search_chroma(query, top_k, filter_metadata)
        elif self.backend == "faiss":
            return self._search_faiss(query, top_k)
        elif self.backend == "memory":
            return self._search_memory(query, top_k)

        return []

    def _search_chroma(
        self,
        query: str,
        top_k: int,
        filter_metadata: Optional[Dict[str, Any]]
    ) -> List[Tuple[str, str, float]]:
        """Search in ChromaDB"""
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=top_k,
                where=filter_metadata
            )

            # Format results
            output = []
            if results['ids'] and len(results['ids']) > 0:
                for i in range(len(results['ids'][0])):
                    doc_id = results['ids'][0][i]
                    text = results['documents'][0][i]
                    distance = results['distances'][0][i] if 'distances' in results else 0.0

                    # Convert distance to similarity (Chroma uses L2 distance)
                    similarity = 1.0 / (1.0 + distance)

                    output.append((doc_id, text, similarity))

            return output

        except Exception as e:
            logger.error(f"Chroma search failed: {e}")
            return []

    def _search_faiss(self, query: str, top_k: int) -> List[Tuple[str, str, float]]:
        """Search in FAISS (uses EmbeddingService with BGE-M3)"""
        import numpy as np

        if self._faiss_index is None or len(self._faiss_metadata) == 0:
            logger.warning("FAISS index is empty")
            return []

        try:
            # Get query embedding using EmbeddingService
            query_embedding = self.embedding_service.embed_query(query)

            # BGE-M3 already returns L2-normalized vectors, but ensure it
            query_array = np.array([query_embedding], dtype=np.float32)
            norm = np.linalg.norm(query_array)
            if norm > 0:
                query_array = query_array / norm

            # Search
            similarities, indices = self._faiss_index.search(
                query_array,
                min(top_k, len(self._faiss_metadata))
            )

            # Format results
            output = []
            for i, idx in enumerate(indices[0]):
                if idx < len(self._faiss_metadata):
                    meta = self._faiss_metadata[idx]
                    similarity = float(similarities[0][i])
                    output.append((meta["id"], meta["text"], similarity))

            return output

        except Exception as e:
            logger.error(f"FAISS search failed: {e}")
            return []

    def _search_memory(self, query: str, top_k: int) -> List[Tuple[str, str, float]]:
        """Memory-only search using cosine similarity if embeddings are available."""
        import numpy as np

        if not self._memory_store:
            return []
        try:
            q_emb = self.embedding_service.embed_query(query)
            q_vec = np.array(q_emb, dtype=np.float32)
            if np.linalg.norm(q_vec) == 0:
                return []
            q_vec = q_vec / np.linalg.norm(q_vec)
            scored: List[Tuple[str, str, float]] = []
            for item in self._memory_store:
                emb = item.get("embedding")
                if emb is None:
                    continue
                v = np.array(emb, dtype=np.float32)
                if np.linalg.norm(v) == 0:
                    continue
                v = v / np.linalg.norm(v)
                sim = float(np.dot(q_vec, v))
                scored.append((item["id"], item["text"], sim))
            scored.sort(key=lambda x: x[2], reverse=True)
            return scored[:top_k]
        except Exception as e:
            logger.warning(f"Memory search failed; returning empty results: {e}")
            return []

    def add_batch(self, documents: List[Dict[str, Any]]):
        """
        Add multiple documents at once.

        Args:
            documents: List of dicts with keys: id, text, metadata (optional)
        """
        for doc in documents:
            self.add(
                doc_id=doc["id"],
                text=doc["text"],
                metadata=doc.get("metadata", {})
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get vector store statistics"""
        if self.backend == "chroma":
            return {
                "backend": "chroma",
                "count": self._collection.count() if self._collection else 0,
                "persist_dir": str(self.persist_dir)
            }
        elif self.backend == "faiss":
            return {
                "backend": "faiss",
                "count": len(self._faiss_metadata),
                "index_size": self._faiss_index.ntotal if self._faiss_index else 0
            }
        elif self.backend == "memory":
            return {
                "backend": "memory",
                "count": len(self._memory_store),
                "note": "fallback backend (no Chroma/FAISS)"
            }

        return {"backend": self.backend, "count": 0}

    def clear(self):
        """Clear all data from vector store"""
        if self.backend == "chroma":
            try:
                self._client.delete_collection("memory_fabric_v35")
            except Exception:
                pass  # Collection might not exist

            from chromadb.utils.embedding_functions import EmbeddingFunction

            class CustomEmbeddingFunction(EmbeddingFunction):
                def __init__(self, embedding_service):
                    self.embedding_service = embedding_service

                def __call__(self, input: List[str]) -> List[List[float]]:
                    return self.embedding_service.embed_documents(input)

            self._collection = self._client.get_or_create_collection(
                "memory_fabric_v35",
                embedding_function=CustomEmbeddingFunction(self.embedding_service)
            )
            logger.info("ChromaDB collection cleared")
        elif self.backend == "faiss":
            self._faiss_index = None
            self._faiss_metadata = []
            logger.info("FAISS index cleared")
        elif self.backend == "memory":
            self._memory_store = []
            logger.info("Memory backend cleared")


# Convenience functions
_default_store = None


def get_vector_store(backend: str = "chroma") -> VectorStore:
    """Get singleton vector store instance"""
    global _default_store
    if _default_store is None or _default_store.backend != backend:
        _default_store = VectorStore(backend=backend)
    return _default_store


def semantic_search(query: str, top_k: int = 5) -> List[Tuple[str, str, float]]:
    """Quick semantic search function"""
    store = get_vector_store()
    return store.search(query, top_k)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test with FAISS (no installation required for demo)
    print("=== Vector Store Demo ===\n")

    # Use FAISS for quick demo
    store = VectorStore(backend="faiss")

    # Add some sample memories
    memories = [
        {
            "id": "mem_001",
            "text": "Data quality issues detected in features_1m_with_labels. Sync triggered.",
            "metadata": {"date": "2025-12-01", "type": "data_issue"}
        },
        {
            "id": "mem_002",
            "text": "HPO experiment completed successfully with Sharpe ratio 1.85",
            "metadata": {"date": "2025-12-02", "type": "success"}
        },
        {
            "id": "mem_003",
            "text": "ClickHouse connection timeout during overnight sync",
            "metadata": {"date": "2025-12-01", "type": "infrastructure"}
        },
        {
            "id": "mem_004",
            "text": "Model performance degraded due to stale features, recommend refresh",
            "metadata": {"date": "2025-11-30", "type": "model_issue"}
        }
    ]

    print("Adding memories...")
    store.add_batch(memories)
    print(f"Added {len(memories)} memories\n")

    # Test semantic search
    test_queries = [
        "我们的数据质量有问题",  # Chinese: "Our data quality has issues"
        "model accuracy is poor",
        "数据库连接失败"  # Chinese: "Database connection failed"
    ]

    for query in test_queries:
        print(f"Query: {query}")
        results = store.search(query, top_k=3)

        for i, (doc_id, text, score) in enumerate(results, 1):
            print(f"  {i}. [{doc_id}] (score: {score:.3f})")
            print(f"     {text[:80]}...")
        print()

    print(f"Stats: {store.get_stats()}")
