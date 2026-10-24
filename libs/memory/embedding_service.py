#!/usr/bin/env python3
"""
Embedding Service: Unified interface for text embeddings

Supports:
- Local BGE-M3 (BAAI/bge-m3) - SOTA multilingual, free, private
- Remote OpenAI/DeepSeek (API) - For cloud deployments

This is a key component of the "Memory Fabric" - enables semantic retrieval
of past lessons, experiments, and trading insights.

Design Philosophy:
- Abstract: Easy to swap providers (local ↔ cloud)
- Efficient: Batch processing, caching
- Safe: Graceful degradation, error handling
"""
from __future__ import annotations

import os
import logging
from typing import List, Optional
from pathlib import Path
import json as _json
import hashlib
import math
import requests

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Abstract base class for embedding services.

    All concrete implementations must support:
    - embed_documents(texts: List[str]) → List[List[float]]
    - embed_query(text: str) → List[float]
    """

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple documents (batch processing).

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (each vector is a list of floats)
        """
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        """
        Embed a single query string.

        Args:
            text: Query string

        Returns:
            Single embedding vector (list of floats)
        """
        raise NotImplementedError


class LocalBGEEmbedding(EmbeddingService):
    """
    Local BGE-M3 model implementation.

    Advantages:
    - Free (no API costs)
    - Private (data never leaves your machine)
    - Multilingual (supports 100+ languages)
    - Long context (up to 8192 tokens)
    - SOTA performance (as of 2024)

    Requirements:
    - pip install sentence-transformers
    - Model download: ~2GB (auto-downloaded on first use)

    Performance:
    - CPU: ~50 docs/sec (M2 Max)
    - GPU: ~500 docs/sec (RTX 4090)
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        device: Optional[str] = None,
        max_length: int = 8192
    ):
        """
        Initialize BGE-M3 model.

        Args:
            model_name: HuggingFace model name
            device: Device to use ('cpu', 'cuda', 'mps', or None for auto-detect)
            max_length: Maximum sequence length (BGE-M3 supports up to 8192)
        """
        self.model = None
        self.max_length = max_length
        self.device = device or "cpu"
        self.is_fallback = False
        self.embedding_dim = 1024

        try:
            from sentence_transformers import SentenceTransformer

            # Auto-detect device if not specified
            if device is None:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
                elif torch.backends.mps.is_available():
                    device = "mps"
                else:
                    device = "cpu"

            self.model = SentenceTransformer(model_name, device=device)
            self.max_length = max_length
            self.device = device

            logger.info(f"Loaded BGE-M3 model: {model_name} on {device}")
            logger.info(f"Max sequence length: {max_length} tokens")

        except ImportError as e:
            self.is_fallback = True
            logger.error("sentence-transformers not installed. Install with: pip install sentence-transformers")
            logger.warning(f"Falling back to deterministic local embedding stub: {e}")
        except Exception as e:
            self.is_fallback = True
            logger.warning(f"Failed to load BGE-M3 model, using deterministic local embedding stub: {e}")

    @staticmethod
    def _normalize(vec: List[float]) -> List[float]:
        norm = math.sqrt(sum(x * x for x in vec))
        if norm <= 0:
            return vec
        return [x / norm for x in vec]

    def _fallback_embed(self, text: str) -> List[float]:
        # Deterministic per-input pseudo-embedding for test/dev environments
        # where sentence-transformers cannot be imported.
        raw = hashlib.sha256((text or "").encode("utf-8")).digest()
        vec = [0.0] * self.embedding_dim
        for i in range(self.embedding_dim):
            b = raw[i % len(raw)]
            val = (float(b) / 127.5) - 1.0
            vec[i] = val
        return self._normalize(vec)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple documents using BGE-M3.

        BGE-M3 automatically:
        - Tokenizes input text
        - Truncates to max_length
        - Normalizes embeddings (L2 norm = 1)

        Returns:
            List of normalized embedding vectors (dim=1024 for BGE-M3)
        """
        if not texts:
            return []

        if self.model is None:
            return [self._fallback_embed(t) for t in texts]

        try:
            # normalize_embeddings=True ensures vectors are L2-normalized
            # This is required for cosine similarity to work correctly
            embeddings = self.model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=len(texts) > 100  # Show progress for large batches
            )

            return embeddings.tolist()

        except Exception as e:
            logger.error(f"Failed to embed {len(texts)} documents: {e}")
            return [self._fallback_embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        """
        Embed a single query string.

        Note: BGE models support query instructions for asymmetric retrieval,
        but for simplicity we use direct encoding (works well in practice).

        For better retrieval quality, you could prepend:
        "Represent this sentence for searching relevant passages: "
        """
        if not text:
            return [0.0] * 1024  # Return zero vector for empty text

        if self.model is None:
            return self._fallback_embed(text)

        try:
            embedding = self.model.encode(
                [text],
                normalize_embeddings=True
            )[0]

            return embedding.tolist()

        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            return self._fallback_embed(text)


class RemoteOpenAIEmbedding(EmbeddingService):
    """
    Remote OpenAI/DeepSeek embedding API.

    Advantages:
    - No local resources required
    - Latest models (text-embedding-3-small/large)
    - Scalable (no GPU needed)

    Disadvantages:
    - Costs money (but cheap: ~$0.02/1M tokens)
    - Requires internet connection
    - Data leaves your machine

    Use cases:
    - Cloud deployments
    - Large-scale batch processing
    - When privacy is not a concern
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-3-small",
        base_url: Optional[str] = None
    ):
        """
        Initialize OpenAI embedding API.

        Args:
            api_key: OpenAI API key (or set OPENAI_API_KEY env var)
            model: Model name (text-embedding-3-small, text-embedding-3-large)
            base_url: Optional base URL for API (for proxies/alternatives)
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url

        if not self.api_key:
            logger.warning("OpenAI API key not found. Set OPENAI_API_KEY environment variable.")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed documents using OpenAI API.

        OpenAI API supports batch processing (up to 2048 texts per request).
        We chunk large batches to avoid hitting limits.
        """
        if not texts:
            return []

        if not self.api_key:
            logger.error("Cannot embed without API key")
            return [[0.0] * 1536 for _ in texts]  # text-embedding-3-small dim=1536

        try:
            import openai

            client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )

            # Batch processing (max 2048 per request)
            BATCH_SIZE = 2048
            all_embeddings = []

            for i in range(0, len(texts), BATCH_SIZE):
                batch = texts[i:i+BATCH_SIZE]

                response = client.embeddings.create(
                    model=self.model,
                    input=batch
                )

                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)

            return all_embeddings

        except Exception as e:
            logger.error(f"Failed to call OpenAI API: {e}")
            return [[0.0] * 1536 for _ in texts]

    def embed_query(self, text: str) -> List[float]:
        """Embed a single query using OpenAI API."""
        if not text:
            return [0.0] * 1536

        embeddings = self.embed_documents([text])
        return embeddings[0] if embeddings else [0.0] * 1536


class OllamaEmbedding(EmbeddingService):
    """
    Local Ollama embedding endpoint wrapper.

    Defaults assume an Ollama server running at http://localhost:11434
    with the bge-m3 model available:
        curl -X POST http://localhost:11434/api/embeddings \
          -d '{"model": "bge-m3", "prompt": "text"}'
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "bge-m3",
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for t in texts:
            out.append(self.embed_query(t))
        return out

    def embed_query(self, text: str) -> List[float]:
        if not text:
            return [0.0] * 1024
        try:
            resp = requests.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.model, "prompt": text},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("embedding")
            if isinstance(emb, list) and emb:
                return [float(x) for x in emb]
        except Exception as e:
            logger.warning(f"Ollama embedding failed: {e}")
        return [0.0] * 1024


def get_embedding_service(
    provider: str = "local_bge",
    config: Optional[dict] = None
) -> EmbeddingService:
    """
    Factory function to create embedding service.

    Args:
        provider: "local_bge" or "openai"
        config: Optional configuration dict

    Returns:
        EmbeddingService instance

    Example:
        >>> service = get_embedding_service("local_bge")
        >>> vectors = service.embed_documents(["hello", "world"])
        >>> len(vectors[0])
        1024  # BGE-M3 dimension
    """
    config = config or {}

    if provider == "local_bge":
        model_config = config.get("local_model", {})
        return LocalBGEEmbedding(
            model_name=model_config.get("name", "BAAI/bge-m3"),
            device=model_config.get("device"),
            max_length=model_config.get("max_length", 8192)
        )

    elif provider == "ollama":
        ollama_cfg = config.get("ollama", {})
        return OllamaEmbedding(
            base_url=ollama_cfg.get("base_url", "http://localhost:11434"),
            model=ollama_cfg.get("model", "bge-m3"),
            timeout=int(ollama_cfg.get("timeout", 30)),
        )

    elif provider == "openai":
        api_config = config.get("openai", {})
        return RemoteOpenAIEmbedding(
            api_key=api_config.get("api_key"),
            model=api_config.get("model", "text-embedding-3-small"),
            base_url=api_config.get("base_url")
        )

    else:
        raise ValueError(f"Unknown embedding provider: {provider}")


def get_embedding_service_from_config(config_path: Optional[str] = None) -> EmbeddingService:
    """
    Load embedding service from config.yml.

    Args:
        config_path: Path to config.yml (defaults to ./config.yml)

    Returns:
        EmbeddingService instance
    """
    import yaml

    if config_path is None:
        config_path = Path("config.yml")

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        memory_config = config.get("memory", {})
        provider = memory_config.get("embedding_provider", "local_bge")

        return get_embedding_service(provider, memory_config)

    except Exception as e:
        logger.warning(f"Failed to load config from {config_path}: {e}")
        logger.info("Falling back to local BGE-M3 (or Ollama if env set)")
        # Allow quick env override without editing config:
        #   export EMBEDDING_PROVIDER=ollama
        #   export OLLAMA_EMBED_MODEL=bge-m3
        provider = os.getenv("EMBEDDING_PROVIDER", "local_bge").lower()
        if provider == "ollama":
            return OllamaEmbedding(
                base_url=os.getenv("OLLAMA_EMBED_BASE", "http://localhost:11434"),
                model=os.getenv("OLLAMA_EMBED_MODEL", "bge-m3"),
            )
        return LocalBGEEmbedding()
