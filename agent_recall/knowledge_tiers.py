"""Knowledge tier manager — 3-tier memory with hot cache injection.

Tier lifecycle:
  cold → warm: access_count >= 5 AND last_accessed < 7d
  warm → hot:  access_count >= 3 AND last_accessed < 7d
  hot → warm:  access_count <= 1 AND last_accessed > 14d
  warm → cold: access_count == 0 AND last_accessed > 30d

Hot cache injection (SessionStart):
  1. Query scope's tier='hot' observations
  2. Sort by salience_score DESC
  3. Return compact format for context injection
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent_recall.decay_engine import DecayEngine, DecayConfig

logger = logging.getLogger("agent_recall.knowledge_tiers")


@dataclass
class TierConfig:
    """Configuration for knowledge tier management.

    Args:
        hot_limit: Maximum hot-cache items per scope.
        hot_promotion_accesses: Accesses to promote to hot.
        hot_demotion_days: Days no-access before hot→warm.
        warm_promotion_accesses: Accesses to promote from cold.
        warm_demotion_days: Days no-access for warm→cold.
        decay_factor: Ebbinghaus decay λ.
    """
    hot_limit: int = 20
    hot_promotion_accesses: int = 3
    hot_demotion_days: int = 14
    warm_promotion_accesses: int = 5
    warm_demotion_days: int = 30
    decay_factor: float = 0.01


class KnowledgeTierManager:
    """Manages the 3-tier memory lifecycle.

    Usage::

        mgr = KnowledgeTierManager(store)
        hot = mgr.get_hot_cache("my-project")  # for SessionStart injection
        mgr.check_and_rebalance("my-project")  # periodic maintenance
    """

    def __init__(
        self,
        store,  # MemoryStore
        decay_engine: DecayEngine | None = None,
        config: TierConfig | None = None,
    ) -> None:
        self._store = store
        self._decay = decay_engine or DecayEngine()
        self.config = config or TierConfig()

    # ------------------------------------------------------------------
    # Hot cache (SessionStart injection)
    # ------------------------------------------------------------------

    def get_hot_cache(self, scope: str) -> list[dict]:
        """Return hot-tier observations for SessionStart injection.

        Returns compact format: {entity_name, type, text, salience, ...}
        """
        items = self._store.get_hot_cache(scope, limit=self.config.hot_limit)

        # If hot cache is under-filled, supplement with high-salience warm items
        if len(items) < 5:
            warm = self._get_top_salience(scope, limit=10)
            existing_ids = {item["id"] for item in items}
            for w in warm:
                if w["id"] not in existing_ids and len(items) < self.config.hot_limit:
                    items.append(w)

        return items

    def _get_top_salience(self, scope: str, limit: int = 10) -> list[dict]:
        """Get top-salience observations regardless of tier."""
        rows = self._store._conn.execute("""
            SELECT o.id, o.entity_id, o.text, o.scope, o.created_at,
                   kt.tier, kt.salience_score, kt.access_count,
                   kt.last_accessed_at, e.name as entity_name, e.type as entity_type
            FROM observations o
            JOIN knowledge_tiers kt ON o.id = kt.observation_id
            JOIN entities e ON o.entity_id = e.id
            WHERE o.scope = ? AND o.archived_at IS NULL
            ORDER BY kt.salience_score DESC LIMIT ?
        """, (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Promotion / Demotion
    # ------------------------------------------------------------------

    def promote(
        self, observation_id: int, to_tier: str, source: str = "manual",
    ) -> None:
        """Promote an observation to a higher tier."""
        valid = {"hot", "warm", "cold"}
        if to_tier not in valid:
            raise ValueError(f"Invalid tier: {to_tier}. Must be one of {valid}")
        self._store.set_tier(observation_id, to_tier, source=source)
        logger.debug("Promoted obs %d to %s (%s)", observation_id, to_tier, source)

    def demote(
        self, observation_id: int, to_tier: str, source: str = "auto",
    ) -> None:
        """Demote an observation to a lower tier."""
        self.promote(observation_id, to_tier, source=source)

    # ------------------------------------------------------------------
    # Periodic rebalancing
    # ------------------------------------------------------------------

    def check_and_rebalance(self, scope: str | None = None) -> dict:
        """Run tier decay check and rebalance. Returns {promoted, demoted}."""
        result = self._decay.bulk_update_tiers(self._store._conn)
        self._store._conn.commit()
        logger.info(
            "Tier rebalance: promoted=%d demoted=%d",
            result["promoted"], result["demoted"],
        )
        return result

    def run_full_maintenance(self, scope: str | None = None) -> dict:
        """Run complete periodic maintenance cycle across all 7 layers.

        Called automatically every ~100 writes + >1h via
        MemoryStore._maybe_maintenance(). Runs:

          1. Tier rebalance (Ebbinghaus decay + salience recalc)
          2. Knowledge promotion (raw→insight→decision→lesson→rule)
          3. Cross-source synthesis (themes + patterns + contradictions)
          4. Trust time decay

        Returns dict with counts for each operation.
        """
        result: dict = {
            "rebalance": {"promoted": 0, "demoted": 0},
            "promotions": 0,
            "synthesis": None,
            "trust_decayed": 0,
        }

        # 1. Tier rebalance
        try:
            result["rebalance"] = self.check_and_rebalance(scope)
        except Exception as e:
            logger.debug("Tier rebalance failed: %s", e)

        # 2. Knowledge promotion (Layer 3)
        try:
            from agent_recall.promotion import KnowledgePromoter
            promoter = KnowledgePromoter(self._store)
            promoted = 0
            # Get observations that are warm+ and not yet promoted
            rows = self._store._conn.execute("""
                SELECT DISTINCT o.id
                FROM observations o
                JOIN knowledge_tiers kt ON o.id = kt.observation_id
                WHERE o.archived_at IS NULL
                  AND kt.tier IN ('hot', 'warm')
                  AND o.id NOT IN (
                      SELECT observation_id FROM trust_events
                      WHERE reason = 'auto_promotion'
                  )
                LIMIT 50
            """).fetchall()
            for row in rows:
                try:
                    if promoter.auto_promote(row["id"]):
                        promoted += 1
                except Exception:
                    pass
            result["promotions"] = promoted
            if promoted:
                logger.info("Auto-promoted %d observations", promoted)
        except Exception as e:
            logger.debug("Promotion cycle failed: %s", e)

        # 3. Cross-source synthesis (Layer 4) — once every ~7 days
        try:
            scope_to_use = scope or "global"
            last_synth = self._store._conn.execute(
                "SELECT MAX(created_at) FROM observations o "
                "JOIN entities e ON o.entity_id = e.id "
                "WHERE e.type = 'synthesis' AND o.scope = ?",
                (scope_to_use,),
            ).fetchone()
            should_synthesize = True
            if last_synth and last_synth[0]:
                try:
                    from datetime import datetime, timedelta, timezone
                    last_dt = datetime.fromisoformat(last_synth[0])
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last_dt).days < 7:
                        should_synthesize = False
                except (ValueError, TypeError):
                    pass
            if should_synthesize:
                from agent_recall.synthesis import Synthesizer
                syn = Synthesizer(self._store)
                result["synthesis"] = syn.synthesize(scope_to_use, since_days=7)
                logger.info("Auto-synthesis completed for scope=%s", scope_to_use)
        except Exception as e:
            logger.debug("Synthesis cycle failed: %s", e)

        # 4. Trust time decay (Layer 7)
        try:
            from agent_recall.trust import TrustEngine
            engine = TrustEngine(self._store)
            result["trust_decayed"] = engine.decay_all()
            if result["trust_decayed"]:
                logger.info("Trust decayed %d observations", result["trust_decayed"])
        except Exception as e:
            logger.debug("Trust decay failed: %s", e)

        self._store._conn.commit()
        return result

    # ------------------------------------------------------------------
    # Salience
    # ------------------------------------------------------------------

    def compute_salience(self, obs_id: int) -> float:
        """Compute current salience for an observation."""
        row = self._store._conn.execute(
            "SELECT access_count, last_accessed_at FROM knowledge_tiers "
            "WHERE observation_id = ?",
            (obs_id,),
        ).fetchone()
        if not row:
            return 0.5
        trust = self._store.get_trust_score(obs_id)
        helpfulness = self._store.get_helpfulness(obs_id)
        return self._decay.compute_salience(
            access_count=row["access_count"] or 0,
            last_accessed=row["last_accessed_at"],
            trust_score=trust,
            helpfulness=helpfulness,
        )

    # ------------------------------------------------------------------
    # Hot cache status
    # ------------------------------------------------------------------

    def status(self, scope: str) -> dict:
        """Return tier distribution stats for a scope."""
        rows = self._store._conn.execute("""
            SELECT kt.tier, COUNT(*) as cnt, AVG(kt.salience_score) as avg_salience
            FROM knowledge_tiers kt
            JOIN observations o ON kt.observation_id = o.id
            WHERE o.scope = ? AND o.archived_at IS NULL
            GROUP BY kt.tier
        """, (scope,)).fetchall()
        tiers = {r["tier"]: {"count": r["cnt"], "avg_salience": round(r["avg_salience"], 3)}
                 for r in rows}
        hot_items = [h["entity_name"] for h in self.get_hot_cache(scope)]
        return {
            "scope": scope,
            "hot_count": len(hot_items),
            "hot_items": hot_items[:10],
            "tiers": tiers,
        }
