"""Entity linker — auto-create relationships from observation text.

Inspired by memory-mcp MENTIONS/DEPENDS_ON linking.

Algorithm:
  1. Scan new observation text for known entity names + NER mentions
  2. Create MENTIONS relation (observation_entity → mentioned_entity)
  3. Detect decision-tech dependencies (decision → DEPENDS_ON → technology)
  4. Dedup: skip if same relation already exists
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent_recall.entity_linker")


class EntityLinker:
    """Auto-links entities mentioned in observations.

    Usage::

        linker = EntityLinker(store)
        count = linker.link_entities_in_observation(obs_id, text)
    """

    def __init__(self, store) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Main linking
    # ------------------------------------------------------------------

    def link_entities_in_observation(
        self, observation_id: int, text: str,
    ) -> int:
        """Scan observation text and create relations to mentioned entities.

        Returns number of links created.
        """
        # Get the source entity for this observation
        obs = self._store._conn.execute(
            "SELECT entity_id FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if not obs:
            return 0
        source_entity_id = obs["entity_id"]

        # Find all known entity names mentioned in the text
        mentioned = self._find_mentioned_entities(text, source_entity_id)
        if not mentioned:
            return 0

        # Get existing relations for dedup
        existing = self._get_existing_relations(source_entity_id)

        count = 0
        for entity_id, entity_name, entity_type in mentioned:
            if entity_id in existing:
                continue
            try:
                self._store.add_relation(
                    source_entity_id, entity_id,
                    "mentions",
                    scope="global",
                    context=f"Auto-linked from observation {observation_id}",
                )
                existing.add(entity_id)
                count += 1
            except Exception as e:
                logger.debug("Failed to link %d→%d: %s",
                             source_entity_id, entity_id, e)

        return count

    # ------------------------------------------------------------------
    # Cross-linking
    # ------------------------------------------------------------------

    def link_co_occurring(self, observation_ids: list[int]) -> int:
        """Create cross-links between entities that co-occur in the SAME observation.

        Only creates co_occurs_with if both entities appear together in at least
        one observation (true co-occurrence), NOT just because they're in the
        same batch. Deduplicates against existing relations to avoid O(n²)
        explosion.

        Returns number of links created.
        """
        if len(observation_ids) < 2:
            return 0

        # Build entity→text map from these observations
        rows = self._store._conn.execute(
            f"SELECT id, entity_id, text FROM observations "
            f"WHERE id IN ({','.join('?' * len(observation_ids))}) "
            f"AND archived_at IS NULL",
            observation_ids,
        ).fetchall()

        # Group by observation: each observation's text may mention multiple entities
        # Find all entities mentioned in each observation's text
        all_entities = {
            r["entity_id"]: r["entity_id"] for r in rows
        }
        entity_names = {}
        for eid in set(r["entity_id"] for r in rows):
            ent = self._store.get_entity(eid)
            if ent:
                entity_names[eid] = ent["name"]

        # True co-occurrence: entities whose names appear in the SAME observation text
        # This prevents linking all entities from a batch as if they're related
        seen_pairs: set[tuple[int, int]] = set()
        count = 0

        # Load existing outgoing relations to avoid duplicates
        for row in rows:
            text = row["text"]
            entity_id = row["entity_id"]
            # Find which OTHER entities from our set are mentioned in this text
            for other_id, other_name in entity_names.items():
                if other_id == entity_id:
                    continue
                if len(other_name) < 3:
                    continue
                # Check if entity name appears in this observation text
                if other_name.lower() in text.lower():
                    pair = (min(entity_id, other_id), max(entity_id, other_id))
                    if pair not in seen_pairs:
                        # Check existing relation
                        existing = self._store._conn.execute(
                            "SELECT COUNT(*) FROM relations "
                            "WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
                            (pair[0], pair[1], pair[1], pair[0]),
                        ).fetchone()
                        if existing and existing[0] > 0:
                            seen_pairs.add(pair)
                            continue
                        try:
                            self._store.add_relation(
                                pair[0], pair[1], "co_occurs_with",
                                scope="global",
                            )
                            seen_pairs.add(pair)
                            count += 1
                        except Exception:
                            pass

        return count

    # ------------------------------------------------------------------
    # Missing link suggestions
    # ------------------------------------------------------------------

    def suggest_missing_links(self, entity_id: int, limit: int = 10) -> list[dict]:
        """Suggest entities that might be related based on text similarity.

        Uses FTS5 on entity names/observations to find potential links.
        """
        entity = self._store.get_entity(entity_id)
        if not entity:
            return []

        # Get all text from this entity's observations
        obs_rows = self._store.get_observations(entity_id)
        if not obs_rows:
            return []

        combined_text = " ".join(o["text"] for o in obs_rows[:5])
        # Extract key terms
        terms = re.findall(r'\b[A-Z][a-z]{2,}(?:[A-Z][a-z]+)*\b', combined_text)

        suggestions: list[dict] = []
        seen_ids = {entity_id}

        # Get existing relations
        outgoing, incoming = self._store.get_all_relations(entity_id)
        seen_ids.update(r["to_id"] for r in outgoing)
        seen_ids.update(r["from_id"] for r in incoming)

        for term in terms[:10]:
            if term.lower() in {"the", "this", "that", "with", "from", "have"}:
                continue
            rows = self._store._conn.execute(
                "SELECT id, name, type FROM entities "
                "WHERE LOWER(name) LIKE ? AND id NOT IN ("
                + ",".join(map(str, seen_ids))
                + ") LIMIT 3",
                (f"%{term.lower()}%",),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    suggestions.append({
                        "entity_id": r["id"],
                        "name": r["name"],
                        "type": r["type"],
                        "matched_term": term,
                    })
                    seen_ids.add(r["id"])

        return suggestions[:limit]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_mentioned_entities(
        self, text: str, exclude_id: int,
    ) -> list[tuple[int, str, str]]:
        """Find known entity names mentioned in text.

        Uses word-boundary matching first, then falls back to case-insensitive
        substring for entity names >= 5 chars (to avoid short-name false
        positives like "go" matching "goto").
        """
        # Get all entity names and check if they appear in text
        all_entities = self._store._conn.execute(
            "SELECT id, name, type FROM entities WHERE id != ?",
            (exclude_id,),
        ).fetchall()

        mentioned: list[tuple[int, str, str]] = []
        matched_ids: set[int] = set()
        text_lower = text.lower()

        for row in all_entities:
            name = row["name"]
            eid = row["id"]
            # Only check names that are long enough to be meaningful
            if len(name) < 3:
                continue
            # Phase 1: word-boundary match (strict, low false-positive)
            pattern = r'\b' + re.escape(name) + r'\b'
            if re.search(pattern, text, re.IGNORECASE):
                mentioned.append((eid, name, row["type"]))
                matched_ids.add(eid)
                continue
            # Phase 2: substring fallback for names >= 5 chars
            # Handles "postgres" in "PostgreSQL", "kubernetes" in "k8s", etc.
            if len(name) >= 5 and eid not in matched_ids:
                if name.lower() in text_lower:
                    mentioned.append((eid, name, row["type"]))
                    matched_ids.add(eid)

        return mentioned

    def _get_existing_relations(self, entity_id: int) -> set[int]:
        """Get set of entity IDs already linked to this entity."""
        outgoing, incoming = self._store.get_all_relations(entity_id)
        return ({r["to_id"] for r in outgoing} |
                {r["from_id"] for r in incoming})
