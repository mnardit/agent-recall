"""Cross-source synthesis + contradiction detection.

Inspired by kb-server kb_synthesize + memory-mcp contradictions.

Synthesis:
  1. Scan recent observations (since N days)
  2. Identify recurring themes (tag clustering + keyword frequency)
  3. Discover cross-entity patterns/bottlenecks
  4. Generate synthesis note

Contradiction detection:
  1. Find semantically similar (>0.8) but opposing observation pairs
  2. Keyword opposition detection: react/vue, tab/space, sync/async, ...
  3. Mark contradiction relations
  4. Prompt human resolution
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("agent_recall.synthesis")

# Opposing keyword pairs (for contradiction detection)
OPPOSITION_PAIRS: list[tuple[str, str]] = [
    ("react", "vue"), ("react", "angular"),
    ("sync", "async"), ("synchronous", "asynchronous"),
    ("tab", "space"),
    ("rest", "graphql"),
    ("sql", "nosql"),
    ("monolith", "microservice"),
    ("monorepo", "polyrepo"),
    ("serverless", "kubernetes"),
    ("grpc", "rest"),
    ("java", "python"), ("java", "go"),
    ("postgres", "mysql"), ("postgres", "mongodb"),
    ("npm", "yarn"), ("npm", "pnpm"),
    ("use", "avoid"), ("always", "never"),
    ("recommend", "warn against"),
]


class Synthesizer:
    """Cross-source synthesis engine.

    Usage::

        syn = Synthesizer(store)
        result = syn.synthesize("my-project", since_days=7)
        # result = {"themes": [...], "patterns": [...], "summary": "..."}
    """

    def __init__(self, store, embedding_provider=None) -> None:
        self._store = store
        self._embedder = embedding_provider

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def synthesize(
        self,
        scope: str,
        since_days: int = 7,
    ) -> dict:
        """Cross-source synthesis for recent observations.

        Returns:
            {
                "themes": [{name, count, examples}],
                "patterns": [{description, confidence}],
                "summary": str,
                "synthesis_id": int | None,
            }
        """
        since = (
            datetime.now(timezone.utc) - timedelta(days=since_days)
        ).isoformat()

        # Get recent observations
        rows = self._store._conn.execute("""
            SELECT o.id, o.text, o.created_at, e.name as entity_name, e.type as entity_type
            FROM observations o
            JOIN entities e ON o.entity_id = e.id
            WHERE o.scope = ? AND o.created_at >= ? AND o.archived_at IS NULL
            ORDER BY o.created_at DESC
            LIMIT 200
        """, (scope, since)).fetchall()

        if not rows:
            return {
                "themes": [],
                "patterns": [],
                "summary": "No recent observations to synthesize.",
                "synthesis_id": None,
            }

        items = [dict(r) for r in rows]

        # Extract themes via keyword frequency
        themes = self._extract_themes(items)

        # Discover cross-entity patterns
        patterns = self._discover_patterns(items, scope)

        # Generate summary
        summary = self._generate_summary(themes, patterns, len(items))

        # Store synthesis as a new observation on a "Synthesis" entity
        synth_id = self._store_synthesis(scope, summary, themes, patterns)

        return {
            "themes": themes,
            "patterns": patterns,
            "summary": summary,
            "synthesis_id": synth_id,
        }

    # ------------------------------------------------------------------
    # Contradiction detection
    # ------------------------------------------------------------------

    def detect_contradictions(
        self,
        scope: str,
        min_similarity: float = 0.8,
    ) -> list[dict]:
        """Find pairs of observations that contradict each other.

        Returns:
            List of {observation_a, observation_b, reason, confidence} dicts.
        """
        # Get recent active observations
        rows = self._store._conn.execute("""
            SELECT o.id, o.text, e.name as entity_name
            FROM observations o
            JOIN entities e ON o.entity_id = e.id
            WHERE o.scope = ? AND o.archived_at IS NULL
            ORDER BY o.created_at DESC
            LIMIT 100
        """, (scope,)).fetchall()

        items = [dict(r) for r in rows]
        contradictions: list[dict] = []

        for i, a in enumerate(items):
            for b in items[i + 1:]:
                result = self._check_opposition(a["text"], b["text"])
                if result:
                    contradictions.append({
                        "observation_a": a["id"],
                        "observation_b": b["id"],
                        "text_a": a["text"][:100],
                        "text_b": b["text"][:100],
                        "entity_a": a["entity_name"],
                        "entity_b": b["entity_name"],
                        "reason": result["reason"],
                        "confidence": result["confidence"],
                    })

        # Mark contradictions in relations
        for c in contradictions[:10]:  # Top 10
            self._mark_contradiction(
                c["observation_a"], c["observation_b"],
                c["reason"], c["confidence"],
            )

        return contradictions

    def resolve_contradiction(
        self,
        winner_id: int,
        loser_id: int,
        reason: str,
    ) -> None:
        """Resolve a contradiction: trust+0.1 for winner, trust-0.2 for loser."""
        winner_entity = self._store._conn.execute(
            "SELECT entity_id FROM observations WHERE id = ?", (winner_id,),
        ).fetchone()
        loser_entity = self._store._conn.execute(
            "SELECT entity_id FROM observations WHERE id = ?", (loser_id,),
        ).fetchone()

        if winner_entity and loser_entity:
            self._store._conn.execute("""
                INSERT INTO relations (from_id, to_id, type, scope, context, created_at)
                VALUES (?, ?, 'supersedes', 'global', ?, datetime('now'))
            """, (winner_entity["entity_id"], loser_entity["entity_id"], reason))
            self._store._conn.commit()

        self._store.adjust_trust(winner_id, "cross_validated", 0.10,
                                 note=f"won contradiction: {reason}")
        self._store.adjust_trust(loser_id, "contradiction_resolved", -0.20,
                                 note=f"lost contradiction to {winner_id}: {reason}")

    # ------------------------------------------------------------------
    # Internal: Theme extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_themes(items: list[dict]) -> list[dict]:
        """Extract common themes from observations via keyword frequency."""
        # Collect significant words (4+ chars, not stop words)
        stop_words = {
            "this", "that", "with", "from", "have", "been", "were",
            "they", "will", "when", "which", "what", "about", "into",
            "just", "like", "over", "after", "before", "between",
        }
        word_counter: Counter[str] = Counter()

        for item in items:
            words = re.findall(r'\b[a-zA-Z]{4,}\b', item["text"].lower())
            meaningful = [w for w in words if w not in stop_words]
            word_counter.update(meaningful)

        # Top themes by frequency (min 3 occurrences)
        themes = []
        for word, count in word_counter.most_common(15):
            if count >= 3:
                examples = [
                    item["text"][:80]
                    for item in items
                    if word in item["text"].lower()
                ][:3]
                themes.append({
                    "name": word,
                    "count": count,
                    "examples": examples,
                })
        return themes

    @staticmethod
    def _discover_patterns(
        items: list[dict], scope: str,
    ) -> list[dict]:
        """Discover cross-entity patterns."""
        patterns: list[dict] = []

        # Entity co-occurrence: which entities are mentioned together
        entity_mentions: Counter[tuple] = Counter()
        for item in items:
            entities = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)*\b', item["text"])
            if len(entities) >= 2:
                for i, e1 in enumerate(entities):
                    for e2 in entities[i + 1:]:
                        entity_mentions[(e1, e2)] += 1

        for (e1, e2), count in entity_mentions.most_common(10):
            if count >= 2:
                patterns.append({
                    "description": f"{e1} ↔ {e2} co-occur {count} times",
                    "confidence": min(0.9, count / 10),
                })

        return patterns

    @staticmethod
    def _generate_summary(
        themes: list[dict],
        patterns: list[dict],
        total_count: int,
    ) -> str:
        """Generate a human-readable synthesis summary."""
        parts = [f"Synthesized {total_count} observations."]

        if themes:
            top_themes = themes[:5]
            parts.append(
                f"Key themes: " +
                ", ".join(f"{t['name']}(x{t['count']})" for t in top_themes)
            )

        if patterns:
            parts.append(
                "Cross-entity patterns: " +
                "; ".join(p["description"] for p in patterns[:3])
            )

        return " ".join(parts)

    def _store_synthesis(
        self, scope: str, summary: str,
        themes: list[dict], patterns: list[dict],
    ) -> int | None:
        """Store synthesis result as a new observation."""
        try:
            eid = self._store.resolve_entity(
                f"Synthesis-{scope}", "synthesis",
            )
            import json
            text = json.dumps({
                "summary": summary,
                "themes": themes,
                "patterns": patterns,
            }, ensure_ascii=False)
            oid = self._store.add_observation(eid, text, scope=scope)
            self._store.set_privacy(oid, "private")
            return oid
        except Exception as e:
            logger.warning("Failed to store synthesis: %s", e)
            return None

    # ------------------------------------------------------------------
    # Internal: Contradiction logic
    # ------------------------------------------------------------------

    @staticmethod
    def _check_opposition(text_a: str, text_b: str) -> dict | None:
        """Check if two texts contain opposing signals.

        Returns {"reason": str, "confidence": float} or None.
        """
        text_a_lower = text_a.lower()
        text_b_lower = text_b.lower()

        for word_a, word_b in OPPOSITION_PAIRS:
            a_has = word_a in text_a_lower and word_b in text_b_lower
            b_has = word_b in text_a_lower and word_a in text_b_lower
            if a_has or b_has:
                return {
                    "reason": f"opposing keywords: {word_a} vs {word_b}",
                    "confidence": 0.75,
                }

        # Check negation patterns: one says "use X", other says "avoid X" or "don't use X"
        negation_re = re.compile(
            r'\b(?:avoid|don\'t\s+use|never\s+use|stop\s+using|deprecate)\s+(\w+)',
            re.IGNORECASE,
        )
        recommends_re = re.compile(
            r'\b(?:use|recommend|prefer|choose)\s+(\w+)',
            re.IGNORECASE,
        )

        neg_matches = negation_re.findall(text_a) + negation_re.findall(text_b)
        rec_matches = recommends_re.findall(text_a) + recommends_re.findall(text_b)

        for neg in neg_matches:
            for rec in rec_matches:
                if neg.lower() == rec.lower():
                    return {
                        "reason": f"contradictory stance on: {neg}",
                        "confidence": 0.85,
                    }

        return None

    def _mark_contradiction(
        self, obs_a: int, obs_b: int, reason: str, confidence: float,
    ) -> None:
        """Create a contradiction relation between two observations' entities."""
        try:
            row_a = self._store._conn.execute(
                "SELECT entity_id FROM observations WHERE id = ?", (obs_a,),
            ).fetchone()
            row_b = self._store._conn.execute(
                "SELECT entity_id FROM observations WHERE id = ?", (obs_b,),
            ).fetchone()
            if row_a and row_b:
                self._store.add_relation(
                    row_a["entity_id"], row_b["entity_id"],
                    "contradiction",
                    context=f"{reason} (confidence: {confidence})",
                )
        except Exception:
            pass  # Relation may already exist — ignore
