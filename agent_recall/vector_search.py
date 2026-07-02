"""Vector search engine — hybrid semantic + keyword retrieval with RRF fusion.

Retrieval flow:
1. query → EmbeddingProvider.embed() → query_vec
2. sqlite-vec KNN (if available): SELECT ... WHERE embedding MATCH ? LIMIT k*3
3. FTS5: SELECT ... WHERE observations_fts MATCH ?
4. Entity boost: exact entity-name match in query
5. RRF (Reciprocal Rank Fusion) → merged ranked results
6. JOIN observations/entities/tiers → full metadata
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict

logger = logging.getLogger("agent_recall.vector_search")


class VectorSearchEngine:
    """Hybrid search engine combining semantic vectors, FTS5, and entity signals.

    Falls back gracefully to FTS5-only when sqlite-vec is unavailable.
    """

    def __init__(
        self,
        store,  # MemoryStore
        embedding_provider=None,  # EmbeddingProvider | None
    ) -> None:
        self._store = store
        self._embedder = embedding_provider
        self._vec_available = self._check_vec()

    def _check_vec(self) -> bool:
        """Check if sqlite-vec extension is loadable."""
        try:
            self._store._conn.execute("SELECT load_extension('vec0')")
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        """Whether vector search is operational."""
        return self._vec_available and self._embedder is not None

    # ------------------------------------------------------------------
    # Pure vector search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.5,
        scope: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        """Semantic vector search only (no FTS5 fusion).

        Args:
            query: Natural language query.
            limit: Max results.
            min_score: Minimum similarity threshold.
            scope: Optional scope filter.
            entity_type: Optional entity type filter.

        Returns:
            List of {id, entity_id, entity_name, text, similarity, ...} dicts.
        """
        if not self.available:
            return self._fts_fallback(query, limit, scope, entity_type)

        query_vec = self._embedder.embed(query)

        # Build SQL for sqlite-vec KNN
        # The vec0 virtual table uses: SELECT * FROM vec0 WHERE embedding MATCH ? LIMIT k
        conditions = []
        params: list = []

        scope_join = ""
        if scope:
            scope_join = "JOIN observations o2 ON ve.observation_id = o2.id"
            conditions.append("o2.scope = ?")
            params.append(scope)

        entity_join = ""
        if entity_type:
            entity_join = "JOIN entities e2 ON ve.entity_id = e2.id"
            conditions.append("e2.type = ?")
            params.append(entity_type)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        try:
            # sqlite-vec query with vector matching
            vec_blob = _vec_to_blob(query_vec)
            rows = self._store._conn.execute(
                f"SELECT ve.observation_id, ve.entity_id, ve.distance "
                f"FROM observation_embeddings ve "
                f"{scope_join} "
                f"{entity_join} "
                f"WHERE ve.embedding MATCH ? "
                f"{'AND ' + ' AND '.join(conditions) if conditions else ''}"
                f"ORDER BY ve.distance LIMIT ?",
                [vec_blob] + params + [limit * 3],
            ).fetchall()
        except Exception as e:
            logger.debug("Vector search failed: %s — falling back to FTS5", e)
            return self._fts_fallback(query, limit, scope, entity_type)

        # Enrich with entity/observation data
        results: list[dict] = []
        for row in rows:
            obs = self._store._conn.execute(
                "SELECT o.id, o.text, o.scope, o.created_at, "
                "e.name as entity_name, e.type as entity_type "
                "FROM observations o JOIN entities e ON o.entity_id = e.id "
                "WHERE o.id = ? AND o.archived_at IS NULL",
                (row["observation_id"],),
            ).fetchone()
            if obs is None:
                continue
            similarity = 1.0 - min(float(row["distance"]), 1.0)
            if similarity >= min_score:
                results.append({
                    **dict(obs),
                    "similarity": round(similarity, 4),
                })

        return results[:limit]

    # ------------------------------------------------------------------
    # Hybrid search (RRF fusion)
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        scope: str | None = None,
    ) -> list[dict]:
        """Hybrid search: semantic + FTS5 + entity boost fused via RRF.

        Args:
            query: Search query.
            limit: Max results.
            scope: Optional scope filter.

        Returns:
            List of {id, entity_id, entity_name, text, similarity, ...} dicts.
        """
        result_lists: list[list[dict]] = []

        # 1. Vector search
        if self.available:
            try:
                vec_results = self.search(
                    query, limit=limit * 2, min_score=0.3, scope=scope,
                )
                result_lists.append(vec_results)
            except Exception as e:
                logger.debug("Vector component failed: %s", e)

        # 2. FTS5 search
        fts_results = self._fts_search_ranked(query, limit * 2, scope)
        result_lists.append(fts_results)

        # 3. Entity name match boost
        entity_results = self._entity_boost(query, scope)
        if entity_results:
            result_lists.append(entity_results)

        # If only one list, return it
        if len(result_lists) == 1:
            return result_lists[0][:limit]

        # RRF fusion
        fused = self._rrf(result_lists, k=60)
        return fused[:limit]

    # ------------------------------------------------------------------
    # FTS5 helpers
    # ------------------------------------------------------------------

    def _fts_fallback(
        self,
        query: str,
        limit: int,
        scope: str | None,
        entity_type: str | None = None,
    ) -> list[dict]:
        """FTS5-only search when vectors are unavailable."""
        if not self._store._has_fts:
            # Ultimate fallback: LIKE search via existing search()
            raw = self._store.search(query, limit)
            enriched = []
            for item in raw:
                obs = self._store.get_observations(item["id"])
                if obs:
                    enriched.append({
                        "id": obs[0]["id"],
                        "entity_id": item["id"],
                        "entity_name": item["name"],
                        "entity_type": item["type"],
                        "text": obs[0]["text"],
                        "scope": obs[0]["scope"],
                        "created_at": obs[0]["created_at"],
                        "similarity": 0.5,  # nominal
                    })
            return enriched[:limit]

        words = query.split()
        fts_terms = " OR ".join(
            f'"{w}"' for w in words if len(w.strip()) >= 2
        )
        if not fts_terms:
            return []

        scope_filter = ""
        scope_params: list = []
        if scope:
            scope_filter = "AND o.scope = ?"
            scope_params = [scope]

        rows = self._store._conn.execute(
            f"SELECT o.id, o.entity_id, o.text, o.scope, o.created_at, "
            f"e.name as entity_name, e.type as entity_type, "
            f"rank "
            f"FROM observations_fts f "
            f"JOIN observations o ON f.rowid = o.id "
            f"JOIN entities e ON o.entity_id = e.id "
            f"WHERE observations_fts MATCH ? "
            f"  AND o.archived_at IS NULL "
            f"  {scope_filter} "
            f"ORDER BY rank LIMIT ?",
            [fts_terms] + scope_params + [limit],
        ).fetchall()

        # Convert rank to similarity (BM25 rank is negative; the closer to 0 the better)
        max_rank = max((abs(r["rank"]) for r in rows), default=1.0) or 1.0
        return [
            {
                "id": r["id"],
                "entity_id": r["entity_id"],
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "text": r["text"],
                "scope": r["scope"],
                "created_at": r["created_at"],
                "similarity": round(1.0 - abs(r["rank"]) / (max_rank * 2), 4),
            }
            for r in rows
        ]

    def _fts_search_ranked(
        self, query: str, limit: int, scope: str | None,
    ) -> list[dict]:
        """FTS5 search returning {id, ...} dicts with rank-based similarity."""
        return self._fts_fallback(query, limit, scope)

    def _entity_boost(
        self, query: str, scope: str | None,
    ) -> list[dict]:
        """Boost results when query tokens match entity names exactly."""
        tokens = [t.strip().lower() for t in query.split() if len(t.strip()) >= 3]
        if not tokens:
            return []

        placeholders = ",".join("?" * len(tokens))
        scope_filter = ""
        params: list = []
        if scope:
            scope_filter = "AND o.scope = ?"
            params.append(scope)

        rows = self._store._conn.execute(
            f"SELECT o.id, o.entity_id, o.text, o.scope, o.created_at, "
            f"e.name as entity_name, e.type as entity_type "
            f"FROM entities e "
            f"JOIN observations o ON e.id = o.entity_id "
            f"WHERE LOWER(e.name) IN ({placeholders}) "
            f"  AND o.archived_at IS NULL "
            f"  {scope_filter} "
            f"ORDER BY o.created_at DESC LIMIT 20",
            tokens + params,
        ).fetchall()

        return [
            {
                "id": r["id"],
                "entity_id": r["entity_id"],
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "text": r["text"],
                "scope": r["scope"],
                "created_at": r["created_at"],
                "similarity": 0.85,  # entity-name match = high confidence
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # RRF — Reciprocal Rank Fusion
    # ------------------------------------------------------------------

    @staticmethod
    def _rrf(
        result_lists: list[list[dict]],
        k: int = 60,
    ) -> list[dict]:
        """Fuse multiple ranked lists via Reciprocal Rank Fusion.

        score = Σ 1/(k + rank_i) for each list i

        Args:
            result_lists: List of ranked result lists.
            k: RRF constant (default 60).

        Returns:
            Merged list sorted by RRF score descending.
        """
        scores: dict[int, float] = defaultdict(float)
        metadata: dict[int, dict] = {}

        for rlist in result_lists:
            seen: set[int] = set()
            rank = 1
            for item in rlist:
                oid = item.get("id")
                if oid is None or oid in seen:
                    continue
                seen.add(oid)
                scores[oid] += 1.0 / (k + rank)
                if oid not in metadata:
                    metadata[oid] = item
                rank += 1

        # Sort by RRF score
        merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        result: list[dict] = []
        for oid, score in merged:
            item = dict(metadata.get(oid, {}))
            item["id"] = oid
            item["rrf_score"] = round(score, 6)
            result.append(item)

        return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _vec_to_blob(vec: list[float]) -> bytes:
    """Convert float list to binary blob (32-bit little-endian)."""
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)
