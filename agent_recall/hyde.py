"""HyDE — Hypothetical Document Embeddings for query expansion.

Inspired by ArcRift. Generates a hypothetical answer for the query,
then uses both query and hypothetical embeddings for retrieval.

Flow:
1. User query: "auth failed how to fix"
2. HyDE generates: "Authentication failed because the JWT token expired..."
3. embed(query) + embed(hypothetical) → dual-vector retrieval
4. Fuse results from both vectors

Caching: Same query's HyDE result cached for 1 hour (by SHA256).
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger("agent_recall.hyde")


class HydeExpander:
    """HyDE query expander with configurable LLM backend.

    Usage::

        hyde = HydeExpander(llm_caller=my_llm_fn)
        hypothetical = hyde.expand("how to fix auth error")
        # Use hypothetical for dual-vector retrieval
    """

    def __init__(
        self,
        llm_caller=None,  # callable(prompt: str) -> str | None
        cache_ttl: int = 3600,
    ) -> None:
        self._llm = llm_caller or self._default_llm
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def expand(self, query: str) -> str | None:
        """Generate hypothetical answer for a query.

        Returns None if LLM is unavailable or generation fails.
        """
        if not query or not query.strip():
            return None

        cache_key = _hash_query(query)
        now = time.time()

        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return cached

        try:
            hypothetical = self._generate(query)
            if hypothetical:
                self._cache[cache_key] = (now, hypothetical)
            return hypothetical
        except Exception as e:
            logger.debug("HyDE generation failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate(self, query: str) -> str | None:
        """Call the LLM to generate a hypothetical answer."""
        prompt = (
            "Given the question, write a brief hypothetical answer that "
            "might appear in a knowledge base. Be specific and factual. "
            "Keep it under 3 sentences.\n\n"
            f"Question: {query}\n"
            "Hypothetical Answer:"
        )
        result = self._llm(prompt)
        if result and isinstance(result, str) and result.strip():
            return result.strip()
        return None

    @staticmethod
    def _default_llm(prompt: str) -> str | None:
        """Default LLM caller (no-op — user must inject one).

        To enable HyDE, pass an llm_caller at init. Example callers:

        - Claude CLI: ``claude -p "{prompt}" --print --output-format text``
        - OpenAI: ``openai.ChatCompletion.create(...)``
        - Any ``def my_llm(prompt: str) -> str: ...``
        """
        logger.debug(
            "HyDE LLM not configured — returning None. "
            "Pass llm_caller to HydeExpander(...) to enable."
        )
        return None

    @property
    def cache_size(self) -> int:
        """Number of cached expansions."""
        return len(self._cache)

    def clear_cache(self) -> None:
        """Clear the HyDE cache."""
        self._cache.clear()


def _hash_query(query: str) -> str:
    """SHA256 hash of normalized query (first 16 hex chars)."""
    normalized = query.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]
