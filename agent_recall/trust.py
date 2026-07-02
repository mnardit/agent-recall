"""Trust scoring system — 10 trust adjustment signals.

Trust initial: 1.0, range [0, 1].

Positive:
  used_correctly: +0.05
  explicitly_confirmed: +0.15
  high_similarity_hit: +0.03
  cross_validated: +0.20

Negative:
  outdated: -0.10
  partially_incorrect: -0.15
  factually_wrong: -0.30
  superseded: -0.05
  low_utility: -0.03
  contradiction_resolved: -0.20

Trust decays with time: trust_with_decay = trust * e^(-0.001 * days_since_adjustment)
"""
from __future__ import annotations

import math
from enum import Enum


class TrustReason(str, Enum):
    USED_CORRECTLY = "used_correctly"
    EXPLICITLY_CONFIRMED = "explicitly_confirmed"
    HIGH_SIMILARITY_HIT = "high_similarity_hit"
    CROSS_VALIDATED = "cross_validated"
    OUTDATED = "outdated"
    PARTIALLY_INCORRECT = "partially_incorrect"
    FACTUALLY_WRONG = "factually_wrong"
    SUPERSEDED = "superseded"
    LOW_UTILITY = "low_utility"
    CONTRADICTION_RESOLVED = "contradiction_resolved"


TRUST_DELTAS: dict[TrustReason, float] = {
    TrustReason.USED_CORRECTLY: 0.05,
    TrustReason.EXPLICITLY_CONFIRMED: 0.15,
    TrustReason.HIGH_SIMILARITY_HIT: 0.03,
    TrustReason.CROSS_VALIDATED: 0.20,
    TrustReason.OUTDATED: -0.10,
    TrustReason.PARTIALLY_INCORRECT: -0.15,
    TrustReason.FACTUALLY_WRONG: -0.30,
    TrustReason.SUPERSEDED: -0.05,
    TrustReason.LOW_UTILITY: -0.03,
    TrustReason.CONTRADICTION_RESOLVED: -0.20,
}


class TrustEngine:
    """Trust scoring engine — wraps MemoryStore trust methods.

    Usage::

        engine = TrustEngine(store)
        new_score = engine.adjust(obs_id, TrustReason.CONFIRMED)
        history = engine.get_history(obs_id)
    """

    def __init__(self, store) -> None:
        self._store = store

    def adjust(
        self,
        observation_id: int,
        reason: TrustReason,
        note: str | None = None,
    ) -> float:
        """Adjust trust and record audit log. Returns new score."""
        delta = TRUST_DELTAS.get(reason, 0.0)
        return self._store.adjust_trust(
            observation_id, reason.value, delta, note=note,
        )

    def get_trust(self, observation_id: int) -> float:
        """Get current trust score."""
        return self._store.get_trust_score(observation_id)

    def get_history(self, observation_id: int) -> list[dict]:
        """Get full trust adjustment history."""
        return self._store.get_trust_history(observation_id)

    def decay_all(self) -> int:
        """Apply time decay to all observations. Returns number updated."""
        rows = self._store._conn.execute("""
            SELECT observation_id, new_trust, created_at
            FROM (
                SELECT observation_id, new_trust, created_at,
                       ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY created_at DESC) as rn
                FROM trust_events
            ) WHERE rn = 1
        """).fetchall()

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        count = 0

        for row in rows:
            try:
                dt = datetime.fromisoformat(row["created_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (now - dt).total_seconds() / 86400.0
                decayed = row["new_trust"] * math.exp(-0.001 * max(0, days))
                if abs(decayed - row["new_trust"]) > 0.001:
                    self._store.adjust_trust(
                        row["observation_id"], "low_utility",
                        decayed - row["new_trust"],
                        note=f"time decay: {days:.0f} days",
                    )
                    count += 1
            except (ValueError, TypeError):
                continue

        return count
