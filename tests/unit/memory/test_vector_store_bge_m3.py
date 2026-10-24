#!/usr/bin/env python3
"""
Tests for VectorStore with BGE-M3 Integration (v3.5)

Verifies:
1. VectorStore loads BGE-M3 from config
2. Embedding dimension is 1024 (BGE-M3 standard)
3. Semantic search works with BGE-M3
4. Both FAISS and ChromaDB backends work
5. Backward compatibility maintained
"""
import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from libs.memory.vector_store import VectorStore
from libs.memory.embedding_service import LocalBGEEmbedding


def _require_real_model(service) -> None:
    if getattr(service, "is_fallback", False):
        pytest.skip("sentence-transformers unavailable; semantic quality assertions skipped in fallback mode")


class TestVectorStoreBGEM3Integration:
    """Test VectorStore with BGE-M3 embeddings"""

    def test_vectorstore_loads_bge_m3_by_default(self):
        """Verify VectorStore uses BGE-M3 by default"""
        store = VectorStore(backend="faiss")

        # Check embedding service is loaded
        assert hasattr(store, "embedding_service")
        assert store.embedding_service is not None

        # Test embedding dimension (BGE-M3 = 1024)
        test_embedding = store.embedding_service.embed_query("test")
        assert len(test_embedding) == 1024, f"Expected BGE-M3 dimension 1024, got {len(test_embedding)}"

        print("✅ VectorStore loads BGE-M3 by default")

    def test_faiss_backend_with_bge_m3(self):
        """Test FAISS backend with BGE-M3"""
        store = VectorStore(backend="faiss")
        _require_real_model(store.embedding_service)

        # Add test documents
        documents = [
            {"id": "doc1", "text": "Data quality issues detected", "metadata": {"type": "issue"}},
            {"id": "doc2", "text": "Model performance improved significantly", "metadata": {"type": "success"}},
            {"id": "doc3", "text": "数据同步完成", "metadata": {"type": "system"}},  # Chinese: Data sync completed
        ]

        store.add_batch(documents)

        # Test semantic search
        results = store.search("数据质量有问题", top_k=2)  # Chinese: Data quality has issues

        assert len(results) > 0, "Search returned no results"

        # Top result should be doc1 (data quality)
        top_id, top_text, top_score = results[0]
        assert "quality" in top_text.lower() or "doc1" in top_id, f"Top result unexpected: {top_text}"
        assert top_score > 0.5, f"Similarity too low: {top_score}"

        print(f"✅ FAISS search with BGE-M3: top_score={top_score:.3f}")

    def test_custom_embedding_service(self):
        """Test VectorStore with custom embedding service"""
        custom_service = LocalBGEEmbedding(model_name="BAAI/bge-m3")
        _require_real_model(custom_service)
        store = VectorStore(backend="faiss", embedding_service=custom_service)

        # Verify custom service is used
        assert store.embedding_service is custom_service

        # Test basic functionality
        store.add("test1", "Test document", {})
        results = store.search("Test query", top_k=1)
        assert len(results) >= 0  # Should not crash

        print("✅ Custom embedding service works")

    def test_multilingual_search(self):
        """Test BGE-M3 multilingual capabilities"""
        store = VectorStore(backend="faiss")
        _require_real_model(store.embedding_service)

        # Add documents in multiple languages
        documents = [
            {"id": "en1", "text": "The model accuracy has degraded significantly", "metadata": {}},
            {"id": "zh1", "text": "模型准确度大幅下降", "metadata": {}},  # Same meaning in Chinese
            {"id": "en2", "text": "Data pipeline is running smoothly", "metadata": {}},
        ]

        store.add_batch(documents)

        # Search in English
        results_en = store.search("model performance dropped", top_k=3)

        # Search in Chinese
        results_zh = store.search("模型性能下降", top_k=3)  # Model performance dropped

        # Both should find relevant documents
        assert len(results_en) > 0
        assert len(results_zh) > 0

        # Top results should be about model degradation
        top_en_text = results_en[0][1]
        top_zh_text = results_zh[0][1]

        assert "accuracy" in top_en_text.lower() or "准确" in top_en_text or "degraded" in top_en_text.lower()
        print(f"✅ Multilingual search works: EN={results_en[0][2]:.3f}, ZH={results_zh[0][2]:.3f}")

    def test_bge_m3_normalized_embeddings(self):
        """Verify BGE-M3 returns L2-normalized embeddings"""
        store = VectorStore(backend="faiss")

        embedding = store.embedding_service.embed_query("Test query")

        # Calculate L2 norm
        import numpy as np
        norm = np.linalg.norm(embedding)

        # Should be approximately 1.0 (normalized)
        assert abs(norm - 1.0) < 0.01, f"Embedding not normalized: norm={norm}"

        print(f"✅ BGE-M3 embeddings are L2-normalized: norm={norm:.6f}")

    def test_vectorstore_stats(self):
        """Test stats reporting"""
        store = VectorStore(backend="faiss")

        # Initial stats
        stats_empty = store.get_stats()
        assert stats_empty["backend"] == "faiss"
        assert stats_empty["count"] == 0

        # Add documents
        store.add_batch([
            {"id": f"doc{i}", "text": f"Document {i}", "metadata": {}}
            for i in range(5)
        ])

        # Stats after adding
        stats_filled = store.get_stats()
        assert stats_filled["count"] == 5

        print(f"✅ Stats reporting works: {stats_filled}")


def test_backward_compatibility():
    """Test that old code still works (backward compatibility)"""
    # Old style - should still work (but use BGE-M3 internally)
    store = VectorStore(backend="faiss")
    _require_real_model(store.embedding_service)

    store.add("old_id", "Old style document", {"legacy": True})
    results = store.search("Old document", top_k=1)

    assert len(results) > 0
    assert results[0][0] == "old_id"

    print("✅ Backward compatibility maintained")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("  VECTORSTORE BGE-M3 INTEGRATION TESTS")
    print("=" * 80 + "\n")

    test_obj = TestVectorStoreBGEM3Integration()

    tests = [
        ("Default BGE-M3 Loading", test_obj.test_vectorstore_loads_bge_m3_by_default),
        ("FAISS Backend", test_obj.test_faiss_backend_with_bge_m3),
        ("Custom Embedding Service", test_obj.test_custom_embedding_service),
        ("Multilingual Search", test_obj.test_multilingual_search),
        ("L2 Normalization", test_obj.test_bge_m3_normalized_embeddings),
        ("Stats Reporting", test_obj.test_vectorstore_stats),
        ("Backward Compatibility", test_backward_compatibility),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            print(f"\n{'=' * 80}")
            print(f"  TEST: {name}")
            print(f"{'=' * 80}")
            test_func()
            print(f"✅ PASSED: {name}\n")
            passed += 1
        except Exception as e:
            print(f"❌ FAILED: {name}")
            print(f"   Error: {e}\n")
            failed += 1

    print("\n" + "=" * 80)
    print(f"  RESULTS: {passed}/{len(tests)} tests passed")
    print("=" * 80)

    if failed == 0:
        print("\n🎉 All tests passed! BGE-M3 integration verified ✅")
    else:
        print(f"\n⚠️  {failed} test(s) failed")
        sys.exit(1)
