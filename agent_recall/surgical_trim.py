"""Surgical sentence trimming — return only matching sentences from a chunk.

Inspired by ArcRift. ~95% noise reduction by keeping only query-relevant
sentences instead of the full chunk.

Algorithm:
1. Split chunk into sentences (regex by [.!?。！？]\\s*)
2. Filter fragments < 5 chars
3. Score each sentence vs query (word overlap / sentence length)
4. Keep top-N matching sentences (default N=3)
5. Fallback: if no matches, return first N sentences
6. Join: "...{s1}... {s2}..."
"""
from __future__ import annotations

import re

# Sentence boundary regex — handles English + CJK punctuation
_SENTENCE_SPLITTER = re.compile(
    r'(?<=[.!?。！？\n])\s*', re.UNICODE,
)
_MIN_SENTENCE_LENGTH = 5
_ELLIPSIS = " ... "


class SurgicalTrimmer:
    """Trims retrieved chunks to only query-matching sentences.

    Args:
        max_sentences: Maximum number of matching sentences to keep.
        fallback_sentences: Number of leading sentences if no match found.
        min_sentence_length: Minimum characters for a sentence to be considered.
    """

    def __init__(
        self,
        max_sentences: int = 3,
        fallback_sentences: int = 3,
        min_sentence_length: int = _MIN_SENTENCE_LENGTH,
    ) -> None:
        self.max_sentences = max_sentences
        self.fallback_sentences = fallback_sentences
        self.min_sentence_length = min_sentence_length

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trim(self, chunk: str, query: str) -> str:
        """Trim a single chunk to matching sentences.

        Args:
            chunk: The full text chunk.
            query: The search query to match against.

        Returns:
            Trimmed string with ellipsis separators.
        """
        if not chunk or not query:
            return chunk or ""

        sentences = self._split_sentences(chunk)
        if not sentences:
            return chunk

        query_tokens = self._tokenize(query)

        # Score each sentence
        scored = [
            (s, self._score_sentence(s, query_tokens))
            for s in sentences
        ]

        # Sort by score desc
        scored.sort(key=lambda x: x[1], reverse=True)

        # Keep top-N with score > 0
        top = [s for s, score in scored if score > 0.0][:self.max_sentences]

        if not top:
            # Fallback: return first N sentences
            top = sentences[:self.fallback_sentences]

        return _ELLIPSIS.join(top)

    def trim_batch(
        self,
        chunks: list[dict],
        query: str,
        text_key: str = "text",
    ) -> list[dict]:
        """Batch-trim multiple chunks in-place.

        Args:
            chunks: List of result dicts with a text field.
            query: The search query.
            text_key: Key for the text field in each dict.

        Returns:
            Same list with text fields trimmed.
        """
        for chunk in chunks:
            if text_key in chunk:
                chunk[text_key] = self.trim(chunk[text_key], query)
        return chunks

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences, filtering short fragments."""
        raw = _SENTENCE_SPLITTER.split(text)
        return [
            s.strip()
            for s in raw
            if len(s.strip()) >= self.min_sentence_length
        ]

    def _score_sentence(self, sentence: str, query_tokens: set[str]) -> float:
        """Score a sentence for relevance to query (word overlap ratio).

        Returns 0.0-1.0.
        """
        sent_tokens = self._tokenize(sentence)
        if not sent_tokens:
            return 0.0
        overlap = len(query_tokens & sent_tokens)
        # Jaccard-like: overlap / max(len(q), len(s))
        denom = max(len(query_tokens), len(sent_tokens))
        return overlap / denom if denom > 0 else 0.0

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Extract lowercase word tokens (3+ chars)."""
        tokens = re.findall(r'\w{3,}', text.lower())
        return set(tokens)
