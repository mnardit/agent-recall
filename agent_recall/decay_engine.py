"""Ebbinghaus decay engine + unified salience scoring.

Salience = w1*access_boost + w2*recency_score + w3*trust_score + w4*helpfulness_score

Decay formula: decayed = base * e^(-λ * days_since_access) + min(access_count * 0.05, 0.3)
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class DecayConfig:
    """Configuration for the Ebbinghaus decay engine.

    Args:
        decay_factor: λ ∈ [0.005, 0.05] — controls decay speed.
        access_boost_cap: Maximum boost from access_count (0.0-1.0).
        access_boost_per: Per-access increment toward cap.
        salience_weights: (access, recency, trust, helpfulness) — must sum to 1.0.
    """
    decay_factor: float = 0.01
    access_boost_cap: float = 0.3
    access_boost_per: float = 0.05
    salience_weights: tuple[float, float, float, float] = (0.25, 0.35, 0.25, 0.15)

    def __post_init__(self) -> None:
        total = sum(self.salience_weights)
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"salience_weights must sum to 1.0, got {total}")


class DecayEngine:
    """Ebbinghaus forgetting curve applied to memory salience.

    Three-tier classification:
        hot  — frequently accessed, recent, high trust
        warm — moderate usage
        cold — stale, rarely accessed

    Promotion/demotion thresholds:
        cold → warm: access_count >= 5 AND last_accessed < 7 days ago
        warm → hot:  access_count >= 3 AND last_accessed < 7 days ago
        hot → warm:  access_count <= 1 AND last_accessed > 14 days ago
        warm → cold: access_count == 0 AND last_accessed > 30 days ago
    """

    def __init__(self, config: DecayConfig | None = None) -> None:
        self.config = config or DecayConfig()

    # ------------------------------------------------------------------
    # Core decay formula
    # ------------------------------------------------------------------

    def compute_decay(
        self,
        base_score: float,
        days_since_access: float,
        access_count: int,
    ) -> float:
        """Apply Ebbinghaus decay to a base score.

        decayed = base * e^(-λ * days) + min(access_count * boost_per, cap)

        Returns value in [0.0, 1.0].
        """
        cfg = self.config
        decayed = base_score * math.exp(-cfg.decay_factor * days_since_access)
        access_boost = min(access_count * cfg.access_boost_per, cfg.access_boost_cap)
        result = decayed + access_boost
        return max(0.0, min(1.0, result))

    # ------------------------------------------------------------------
    # Recency
    # ------------------------------------------------------------------

    @staticmethod
    def compute_recency(last_accessed: str | None) -> float:
        """Score recency as 0-1: today=1, 365 days ago ≈ 0.

        Uses exponential decay with λ=0.01 (same curve as main decay).
        """
        if last_accessed is None:
            return 0.0
        try:
            dt = datetime.fromisoformat(last_accessed)
        except (ValueError, TypeError):
            return 0.0
        now = datetime.now(timezone.utc)
        # If dt is naive, assume UTC
        if dt.tzinfo is None:
            from datetime import timezone as tz
            dt = dt.replace(tzinfo=tz.utc)
        days = (now - dt).total_seconds() / 86400.0
        return math.exp(-0.01 * max(0.0, days))

    # ------------------------------------------------------------------
    # Unified salience
    # ------------------------------------------------------------------

    def compute_salience(
        self,
        access_count: int,
        last_accessed: str | None,
        trust_score: float,
        helpfulness: float,
    ) -> float:
        """Unified salience = weighted sum of four signals.

        All inputs should be normalized to [0, 1].
        """
        w = self.config.salience_weights
        access_norm = min(access_count / 20.0, 1.0)  # cap at 20 accesses
        recency = self.compute_recency(last_accessed)
        return (
            w[0] * access_norm
            + w[1] * recency
            + w[2] * trust_score
            + w[3] * helpfulness
        )

    # ------------------------------------------------------------------
    # Tier logic
    # ------------------------------------------------------------------

    def should_promote(
        self,
        obs_id: int,
        current_tier: str,
        access_count: int = 0,
        last_accessed: str | None = None,
    ) -> tuple[bool, str]:
        """Determine if an observation should change tiers.

        Returns (should_change, new_tier).
        """
        days = 999.0
        if last_accessed is not None:
            try:
                dt = datetime.fromisoformat(last_accessed)
                if dt.tzinfo is None:
                    from datetime import timezone as tz
                    dt = dt.replace(tzinfo=tz.utc)
                days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
            except (ValueError, TypeError):
                pass

        if current_tier == "cold":
            if access_count >= 5 and days < 7:
                return (True, "warm")
        elif current_tier == "warm":
            if access_count >= 3 and days < 7:
                return (True, "hot")
            elif access_count == 0 and days > 30:
                return (True, "cold")
        elif current_tier == "hot":
            if access_count <= 1 and days > 14:
                return (True, "warm")

        return (False, current_tier)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def bulk_update_tiers(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Recompute salience for all observations and adjust tiers.

        Returns {"promoted": N, "demoted": N}.
        """
        promoted = 0
        demoted = 0

        rows = conn.execute("""
            SELECT
                o.id,
                COALESCE(kt.tier, 'warm') AS tier,
                COALESCE(kt.access_count, 0) AS access_count,
                kt.last_accessed_at,
                COALESCE(kt.base_importance, 0.5) AS base_importance,
                COALESCE(
                    (SELECT AVG(CASE WHEN te.reason IN ('confirmed','used_correctly','cross_validated')
                                THEN te.new_trust ELSE NULL END)
                     FROM trust_events te WHERE te.observation_id = o.id),
                    1.0
                ) AS trust_score,
                COALESCE(
                    (SELECT (CAST(SUM(CASE WHEN re.was_used=1 THEN 1 ELSE 0 END) AS REAL) + 1.0)
                            / (CAST(COUNT(*) AS REAL) + 3.0)
                     FROM retrieval_events re WHERE re.observation_id = o.id),
                    0.25
                ) AS helpfulness
            FROM observations o
            LEFT JOIN knowledge_tiers kt ON o.id = kt.observation_id
            WHERE o.archived_at IS NULL
        """).fetchall()

        for row in rows:
            oid = row["id"]
            current_tier = row["tier"]
            access_count = row["access_count"]
            last_accessed = row["last_accessed_at"]
            trust = row["trust_score"]
            helpfulness = row["helpfulness"]

            salience = self.compute_salience(access_count, last_accessed, trust, helpfulness)
            should_change, new_tier = self.should_promote(
                oid, current_tier, access_count, last_accessed
            )

            # Upsert knowledge_tiers
            conn.execute("""
                INSERT INTO knowledge_tiers
                    (observation_id, tier, salience_score, access_count,
                     last_accessed_at, base_importance, decay_factor)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    tier = COALESCE(?, tier),
                    salience_score = ?,
                    access_count = access_count,
                    last_accessed_at = COALESCE(?, last_accessed_at),
                    base_importance = base_importance
            """, (
                oid, new_tier if should_change else current_tier,
                salience, access_count, last_accessed,
                row["base_importance"], self.config.decay_factor,
                new_tier if should_change else None,
                salience,
                last_accessed,
            ))

            if should_change:
                if new_tier in ("hot", "warm") and current_tier in ("warm", "cold"):
                    promoted += 1
                else:
                    demoted += 1

        return {"promoted": promoted, "demoted": demoted}
