#!/usr/bin/env python3
"""
Memory Retrieval Module (Meta v3.2)

Provides simple keyword-based memory retrieval for Orchestrator/Brain
to inject historical context into decision-making.

This is the first step toward full vector-based semantic retrieval (Meta v3.3).
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    Memory retrieval system with semantic search capabilities.

    Meta v3.2: Keyword-based retrieval
    Meta v3.3: ✅ Vector-based semantic search (Chroma/FAISS)
    """

    def __init__(
        self,
        summaries_path: Optional[Path] = None,
        use_vector_search: bool = True,
        vector_backend: str = "faiss"
    ):
        self.summaries_path = summaries_path or Path("runs/memory/daily_summaries.jsonl")
        self.use_vector_search = use_vector_search
        self.vector_backend = vector_backend
        self._vector_store = None

        # Initialize vector store if enabled
        if self.use_vector_search:
            try:
                from libs.memory.vector_store import VectorStore
                self._vector_store = VectorStore(backend=vector_backend)
                self._sync_to_vector_store()
            except Exception as e:
                logger.warning(f"Vector store initialization failed: {e}. Falling back to keyword search.")
                self.use_vector_search = False

    def get_recent_summaries(self, days: int = 7) -> List[Dict[str, Any]]:
        """
        Get daily summaries from the last N days.

        Args:
            days: Number of days to look back

        Returns:
            List of summary dicts, sorted by date (oldest first)
        """
        if not self.summaries_path.exists():
            logger.warning(f"Summaries file not found: {self.summaries_path}")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        summaries = []

        try:
            with self.summaries_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        summary = json.loads(line)

                        # Check date
                        date_str = summary.get("date")
                        if date_str:
                            summary_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                            if summary_date >= cutoff:
                                summaries.append(summary)
                    except Exception as e:
                        logger.warning(f"Failed to parse summary line: {e}")
                        continue

            # Sort by date
            summaries.sort(key=lambda x: x.get("date", ""))

        except Exception as e:
            logger.error(f"Failed to read summaries: {e}")
            return []

        return summaries

    def _sync_to_vector_store(self):
        """
        Meta v3.3: Sync daily summaries to vector store for semantic search.

        This runs on initialization to ensure vector store is up-to-date.
        """
        if not self._vector_store or not self.summaries_path.exists():
            return

        try:
            with self.summaries_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        summary = json.loads(line)
                        doc_id = summary.get("date", "unknown")

                        # Combine all text for embedding
                        text_parts = [
                            summary.get("summary", ""),
                            " ".join(summary.get("key_observations", [])),
                            " ".join(summary.get("lessons_learned", [])),
                            " ".join(summary.get("recommendations", []))
                        ]
                        full_text = " ".join(p for p in text_parts if p)

                        if full_text:
                            self._vector_store.add(
                                doc_id=doc_id,
                                text=full_text,
                                metadata={"date": doc_id}
                            )
                    except json.JSONDecodeError:
                        continue

            stats = self._vector_store.get_stats()
            logger.info(f"Synced {stats.get('count', 0)} summaries to vector store ({stats.get('backend')})")

        except Exception as e:
            logger.warning(f"Failed to sync to vector store: {e}")

    def search_by_keyword(self, keyword: str, days: int = 30) -> List[Dict[str, Any]]:
        """
        Search summaries containing a specific keyword (case-insensitive).

        Args:
            keyword: Keyword to search for
            days: Number of days to look back

        Returns:
            List of matching summaries
        """
        summaries = self.get_recent_summaries(days)
        keyword_lower = keyword.lower()

        matching = []
        for summary in summaries:
            # Search in summary text
            summary_text = summary.get("summary", "").lower()
            if keyword_lower in summary_text:
                matching.append(summary)
                continue

            # Search in key observations
            observations = summary.get("key_observations", [])
            if any(keyword_lower in obs.lower() for obs in observations):
                matching.append(summary)
                continue

            # Search in lessons learned
            lessons = summary.get("lessons_learned", [])
            if any(keyword_lower in lesson.lower() for lesson in lessons):
                matching.append(summary)
                continue

        return matching

    def search_semantic(
        self,
        query: str,
        top_k: int = 5,
        days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Meta v3.3: Semantic search using vector embeddings.

        This finds summaries that are semantically similar to the query,
        even if they don't share exact keywords.

        Args:
            query: Query text (can be in any language)
            top_k: Number of results to return
            days: Optional filter by recent days

        Returns:
            List of matching summaries with similarity scores
        """
        if not self.use_vector_search or not self._vector_store:
            logger.warning("Vector search not available, falling back to keyword search")
            # Extract likely keywords from query for fallback
            keywords = query.split()[:3]  # Use first 3 words
            if keywords:
                return self.search_by_keyword(keywords[0], days or 30)
            return []

        try:
            # Perform semantic search
            results = self._vector_store.search(query, top_k)

            # Convert vector results back to full summaries
            summaries = []
            all_summaries = self.get_recent_summaries(days or 365)  # Get all if no day filter
            summary_by_date = {s.get("date"): s for s in all_summaries}

            for doc_id, text, score in results:
                if doc_id in summary_by_date:
                    summary = summary_by_date[doc_id].copy()
                    summary["_similarity_score"] = score
                    summaries.append(summary)

            return summaries

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    def get_lessons_learned(self, days: int = 14) -> List[str]:
        """
        Extract all lessons learned from recent summaries.

        This is useful for injecting accumulated wisdom into LLM prompts.

        Args:
            days: Number of days to look back

        Returns:
            List of lesson strings
        """
        summaries = self.get_recent_summaries(days)
        lessons = []

        for summary in summaries:
            date = summary.get("date", "unknown")
            for lesson in summary.get("lessons_learned", []):
                lessons.append(f"[{date[:10]}] {lesson}")

        return lessons

    def get_recommendations(self, days: int = 7) -> List[str]:
        """
        Extract all outstanding recommendations from recent summaries.

        Args:
            days: Number of days to look back

        Returns:
            List of recommendation strings
        """
        summaries = self.get_recent_summaries(days)
        recommendations = []

        for summary in summaries:
            date = summary.get("date", "unknown")
            for rec in summary.get("recommendations", []):
                recommendations.append(f"[{date[:10]}] {rec}")

        return recommendations

    def format_context_for_llm(self, days: int = 7, max_summaries: int = 5) -> str:
        """
        Format recent memory context for LLM injection.

        This provides the "似曾相识" (déjà vu) context that helps
        the system learn from past experiences.

        Args:
            days: Number of days to look back
            max_summaries: Maximum number of summaries to include

        Returns:
            Formatted context string for LLM prompt
        """
        summaries = self.get_recent_summaries(days)

        if not summaries:
            return "No recent memory available."

        # Take the most recent N summaries
        recent = summaries[-max_summaries:]

        lines = ["## Recent Memory Context (Last 7 Days)\n"]

        for summary in recent:
            date = summary.get("date", "unknown")[:10]
            text = summary.get("summary", "")
            observations = summary.get("key_observations", [])
            lessons = summary.get("lessons_learned", [])

            lines.append(f"### {date}")
            lines.append(f"{text}\n")

            if observations:
                lines.append("**Key Observations:**")
                for obs in observations:
                    lines.append(f"- {obs}")

            if lessons:
                lines.append("**Lessons Learned:**")
                for lesson in lessons:
                    lines.append(f"- {lesson}")

            lines.append("")  # Blank line

        return "\n".join(lines)


# Convenience functions for quick access
_retriever = None


def get_retriever() -> MemoryRetriever:
    """Get singleton MemoryRetriever instance"""
    global _retriever
    if _retriever is None:
        _retriever = MemoryRetriever()
    return _retriever


def get_recent_context(days: int = 7) -> str:
    """Quick function to get formatted LLM context"""
    return get_retriever().format_context_for_llm(days)


def search_memory(keyword: str, days: int = 30) -> List[Dict[str, Any]]:
    """Quick function to search memory by keyword"""
    return get_retriever().search_by_keyword(keyword, days)


def search_memory_semantic(query: str, top_k: int = 5, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Meta v3.3: Quick function for semantic search.

    Example:
        results = search_memory_semantic("我们的数据质量有问题", top_k=3)
        # Returns summaries about data quality issues, even in Chinese
    """
    return get_retriever().search_semantic(query, top_k, days)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    retriever = MemoryRetriever()

    print("=== Recent Summaries ===")
    summaries = retriever.get_recent_summaries(days=7)
    print(f"Found {len(summaries)} summaries")

    print("\n=== Lessons Learned ===")
    lessons = retriever.get_lessons_learned(days=14)
    for lesson in lessons:
        print(lesson)

    print("\n=== LLM Context ===")
    context = retriever.format_context_for_llm(days=7)
    print(context)

    print("\n=== Search: 'data quality' ===")
    results = retriever.search_by_keyword("data quality", days=30)
    print(f"Found {len(results)} matching summaries")
