"""Retrieval feedback loop — Bayesian helpfulness scoring.

Bayesian posterior: helpfulness = (used + α) / (retrieved + α + β)
  α=1, β=3 (conservative prior: starts at 0.25)

Tracks which memories are actually useful after retrieval.
"""
from __future__ import annotations


class RetrievalFeedback:
    """Retrieval feedback tracker.

    Usage::

        fb = RetrievalFeedback(store)
        event_id = fb.log_hit("auth error fix", obs_id, 0.85)
        fb.log_use(event_id, was_used=True)
    """

    def __init__(self, store) -> None:
        self._store = store

    def log_hit(
        self, query: str, observation_id: int, similarity: float,
    ) -> int:
        """Log that an observation was retrieved for a query."""
        return self._store.log_retrieval(query, observation_id, similarity)

    def log_use(
        self, retrieval_id: int, was_used: bool,
        feedback: str | None = None,
    ) -> None:
        """Record whether the retrieved observation was actually used.

        When was_used=True, also increments access_count on the
        knowledge_tiers row to drive hot/warm promotion.
        """
        self._store.log_usage(retrieval_id, was_used, feedback)
        if was_used:
            row = self._store._conn.execute(
                "SELECT observation_id FROM retrieval_events WHERE id = ?",
                (retrieval_id,),
            ).fetchone()
            if row and row["observation_id"]:
                self._store.update_access(row["observation_id"])

    def get_helpfulness(self, observation_id: int) -> float:
        """Bayesian helpfulness score."""
        return self._store.get_helpfulness(observation_id)

    def get_top_performers(self, limit: int = 20) -> list[dict]:
        """Get most helpful memories."""
        rows = self._store._conn.execute("""
            SELECT o.id, o.text, e.name as entity_name,
                   COUNT(re.id) as times_retrieved,
                   SUM(CASE WHEN re.was_used = 1 THEN 1 ELSE 0 END) as times_used,
                   CAST(SUM(CASE WHEN re.was_used = 1 THEN 1 ELSE 0 END) + 1 AS REAL)
                   / CAST(COUNT(*) + 4 AS REAL) as helpfulness
            FROM observations o
            JOIN entities e ON o.entity_id = e.id
            LEFT JOIN retrieval_events re ON o.id = re.observation_id
            WHERE o.archived_at IS NULL
            GROUP BY o.id
            HAVING times_retrieved >= 3
            ORDER BY helpfulness DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(r) for r in rows]
