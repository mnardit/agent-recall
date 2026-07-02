"""Knowledge promotion chain: raw → insight → decision → lesson → rule.

Inspired by kb-server promotion pipeline.

Promotion rules:
  1. Same pattern appears >= 3 times → boost confidence
  2. Same fact confirmed by >= 2 independent agents → boost
  3. Observation tagged "decision" → auto +0.2 confidence
  4. Contains causal language ("because", "since") → +0.1 confidence
  5. Threshold reached → candidate rule (needs human gate)

Knowledge type hierarchy (by value):
  raw < insight < decision < lesson < rule
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("agent_recall.promotion")

# Causal reasoning markers
CAUSAL_PATTERNS = re.compile(
    r'\b(because|since|therefore|thus|hence|as a result|'
    r'consequently|leads to|results in|causes)\b',
    re.IGNORECASE,
)

# Knowledge type hierarchy order
TYPE_HIERARCHY = {
    "raw": 0,
    "insight": 1,
    "decision": 2,
    "lesson": 3,
    "rule": 4,
}


@dataclass
class PromotionResult:
    """Result of a promotion evaluation."""
    observation_id: int
    promotable: bool
    current_type: str
    target_type: str
    confidence: float
    reason: str
    needs_human_gate: bool = False


class KnowledgePromoter:
    """Evaluates and executes knowledge promotion.

    Usage::

        promoter = KnowledgePromoter(store)
        result = promoter.evaluate_promotability(obs_id)
        if result.promotable and not result.needs_human_gate:
            promoter.auto_promote(obs_id)
    """

    def __init__(self, store, config=None) -> None:  # AutoCaptureConfig | None
        self._store = store
        self._config = config

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate_promotability(self, observation_id: int) -> PromotionResult:
        """Evaluate if an observation is ready for promotion.

        Returns:
            PromotionResult with promotable flag, target type, and rationale.
        """
        # Get observation and current tier info
        obs = self._store._conn.execute(
            "SELECT id, entity_id, text, scope, created_at "
            "FROM observations WHERE id = ? AND archived_at IS NULL",
            (observation_id,),
        ).fetchone()
        if not obs:
            return PromotionResult(
                observation_id=observation_id,
                promotable=False, current_type="raw", target_type="raw",
                confidence=0.0, reason="Observation not found or archived",
            )

        text = obs["text"]
        current_type = self._infer_current_type(observation_id)
        confidence = 0.5
        reasons: list[str] = []

        # Rule 1: Pattern occurrence count
        pattern_count = self._count_related_patterns(text)
        if pattern_count >= 3:
            confidence += 0.15
            reasons.append(f"pattern seen {pattern_count} times")

        # Rule 2: Cross-agent confirmation
        agent_count = self._count_distinct_agents(observation_id)
        if agent_count >= 2:
            confidence += 0.20
            reasons.append(f"confirmed by {agent_count} agents")

        # Rule 3: "decision" entity tag
        entity_type = self._store._conn.execute(
            "SELECT e.type FROM entities e "
            "JOIN observations o ON e.id = o.entity_id "
            "WHERE o.id = ?", (observation_id,),
        ).fetchone()
        if entity_type and entity_type[0] == "decision":
            confidence += 0.20
            reasons.append("tagged as decision")

        # Rule 4: Causal reasoning
        if CAUSAL_PATTERNS.search(text):
            confidence += 0.10
            reasons.append("contains causal reasoning")

        # Cap confidence
        confidence = min(1.0, confidence)

        # Determine target type
        target_type = self._next_type(current_type, confidence)
        promotable = target_type != current_type and confidence >= 0.6
        needs_human = target_type in ("rule", "lesson")

        return PromotionResult(
            observation_id=observation_id,
            promotable=promotable,
            current_type=current_type,
            target_type=target_type,
            confidence=confidence,
            reason="; ".join(reasons) if reasons else "no promotion triggers met",
            needs_human_gate=needs_human,
        )

    # ------------------------------------------------------------------
    # Auto-promote
    # ------------------------------------------------------------------

    def auto_promote(self, observation_id: int) -> bool:
        """Auto-promote if criteria met (stops at human gate)."""
        result = self.evaluate_promotability(observation_id)
        if not result.promotable:
            return False
        if result.needs_human_gate:
            logger.info(
                "Observation %d needs human gate for %s: %s",
                observation_id, result.target_type, result.reason,
            )
            return False

        # Execute promotion: update entity type
        self._set_entity_type_for_obs(observation_id, result.target_type)
        self._store.set_tier(observation_id, "hot", source="auto_promotion")
        logger.info(
            "Auto-promoted obs %d: %s → %s (%.2f)",
            observation_id, result.current_type, result.target_type,
            result.confidence,
        )
        return True

    # ------------------------------------------------------------------
    # Rule candidates
    # ------------------------------------------------------------------

    def collect_rule_candidates(self, min_confidence: float = 0.7) -> list[dict]:
        """Collect observations that reached rule threshold (needs human gate)."""
        rows = self._store._conn.execute("""
            SELECT o.id, o.text, e.name as entity_name, e.type as entity_type
            FROM observations o
            JOIN entities e ON o.entity_id = e.id
            WHERE o.archived_at IS NULL
              AND (e.type IN ('decision', 'lesson')
                   OR o.text LIKE '%best practice%'
                   OR o.text LIKE '%rule%'
                   OR o.text LIKE '%always%'
                   OR o.text LIKE '%never%')
            ORDER BY o.created_at DESC
            LIMIT 50
        """).fetchall()

        candidates = []
        for row in rows:
            result = self.evaluate_promotability(row["id"])
            if result.confidence >= min_confidence:
                candidates.append({
                    "id": row["id"],
                    "text": row["text"],
                    "entity_name": row["entity_name"],
                    "current_type": result.current_type,
                    "target_type": result.target_type,
                    "confidence": result.confidence,
                    "reason": result.reason,
                })
        return candidates

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _infer_current_type(self, observation_id: int) -> str:
        """Infer the current knowledge type from entity type."""
        row = self._store._conn.execute(
            "SELECT e.type FROM entities e "
            "JOIN observations o ON e.id = o.entity_id "
            "WHERE o.id = ?", (observation_id,),
        ).fetchone()
        if row and row[0] in TYPE_HIERARCHY:
            return row[0]
        return "raw"

    @staticmethod
    def _next_type(current: str, confidence: float) -> str:
        """Determine next promotion target based on current + confidence.

        Thresholds:
          >= 0.90 → double jump (e.g. raw → decision)
          >= 0.65 → single jump (e.g. raw → insight, insight → decision)
          >= 0.70 + current is decision + human_gate → lesson
        """
        order = list(TYPE_HIERARCHY.keys())
        try:
            idx = order.index(current)
        except ValueError:
            idx = 0

        if confidence >= 0.90 and idx < len(order) - 1:
            return order[min(idx + 2, len(order) - 1)]  # double jump
        elif confidence >= 0.65 and idx < len(order) - 1:
            return order[idx + 1]  # single jump
        return current

    def _count_related_patterns(self, text: str) -> int:
        """Count how many times similar patterns appear in pattern_store."""
        import hashlib
        h = hashlib.sha256(text[:200].encode()).hexdigest()
        row = self._store._conn.execute(
            "SELECT COALESCE(occurrence_count, 0) FROM pattern_store WHERE pattern_hash = ?",
            (h,),
        ).fetchone()
        return row[0] if row else 0

    def _count_distinct_agents(self, observation_id: int) -> int:
        """Count distinct agents that confirmed this observation."""
        row = self._store._conn.execute(
            "SELECT COUNT(DISTINCT tagged_by) FROM observation_privacy "
            "WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return row[0] if row else 0

    def _set_entity_type_for_obs(self, observation_id: int, new_type: str) -> None:
        """Update the entity type to reflect promoted knowledge type."""
        row = self._store._conn.execute(
            "SELECT entity_id FROM observations WHERE id = ?", (observation_id,),
        ).fetchone()
        if row:
            entity_id = row["entity_id"]
            current = self._store._conn.execute(
                "SELECT type FROM entities WHERE id = ?", (entity_id,),
            ).fetchone()
            if current and TYPE_HIERARCHY.get(new_type, 0) > TYPE_HIERARCHY.get(current[0], 0):
                self._store._conn.execute(
                    "UPDATE entities SET type = ? WHERE id = ?",
                    (new_type, entity_id),
                )
                self._store._conn.commit()
