"""Access pattern prediction — Markov transition probability.

Algorithm:
  1. Record memory_A → memory_B transitions during retrieval sessions
  2. Build transition matrix: P(B|A) = count(A→B) / total_from(A)
  3. Predict top-K next observations when current is accessed
  4. Pre-warm: add predictions to hot cache (source='predicted')
  5. Decay stale transition probabilities over time
"""
from __future__ import annotations


class AccessPredictor:
    """Markov-chain memory access predictor.

    Usage::

        pred = AccessPredictor(store)
        pred.record_transition(from_obs_id, to_obs_id)
        next_items = pred.predict(current_obs_id, top_k=5)
    """

    def __init__(self, store) -> None:
        self._store = store

    def record_transition(self, from_id: int, to_id: int) -> None:
        """Record a navigation event."""
        self._store.record_transition(from_id, to_id)

    def predict(self, current_id: int, top_k: int = 5) -> list[dict]:
        """Predict next likely observations."""
        return self._store.predict_next(current_id, top_k)

    def pre_warm(self, current_id: int) -> int:
        """Pre-warm hot cache with predicted next items. Returns count."""
        predictions = self.predict(current_id, top_k=5)
        count = 0
        for p in predictions:
            if p.get("probability", 0) > 0.3:
                self._store.set_tier(
                    p["to_observation_id"], "hot", source="predicted",
                )
                count += 1
        return count

    def decay_probabilities(self) -> None:
        """Decay stale transition probabilities (half-life: 14 days)."""
        self._store._conn.execute("""
            UPDATE access_patterns
            SET probability = probability * 0.95
            WHERE last_seen < datetime('now', '-7 days')
        """)
        self._store._conn.commit()
