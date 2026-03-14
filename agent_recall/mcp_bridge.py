"""MCP compatibility bridge — translates MCP memory protocol to frame store.

Maps @modelcontextprotocol/server-memory API to our SQLite store.
Observations stored in dedicated table (not as slots).
Scope enforcement: agents can only write to entities in their scope tree.
"""
from pathlib import Path

from agent_recall.config import MemoryConfig
from agent_recall.store import MemoryStore


class MCPBridge:
    """Bridge between MCP memory protocol and the SQLite store.

    Translates MCP tool calls (create_entities, add_observations, etc.) into
    MemoryStore operations. Enforces scope isolation: agents can only write to
    entities within their scope tree.

    Args:
        db_path: Path to SQLite database.
        default_scope: Scope for new observations/slots (e.g. "acme").
        scope_chain: Agent's scope chain (e.g. ["global", "acme", "proj-a"]).
            Used for scope enforcement — agents with chain length > 1 can only
            write to entities in their allowed scopes.
        config: Optional MemoryConfig for hierarchy lookups.
        scope_reads: Whether to filter search results by scope (default True).
            Set to False for orchestrators or admin agents that need
            cross-scope visibility in search_nodes().

    Example::

        bridge = MCPBridge("memory.db", default_scope="acme",
                           scope_chain=["global", "acme"])
        bridge.create_entities([{"name": "Alice", "entityType": "person",
                                 "observations": ["New team member"]}])
    """

    MAX_ENTITY_NAME = 500
    MAX_OBSERVATION_TEXT = 10000
    MAX_ENTITY_TYPE = 50
    MAX_ITEMS_PER_CALL = 100
    MAX_RELATION_TYPE = 200
    MAX_RELATIONS = 5000

    def __init__(self, db_path: Path | str, default_scope: str = "global",
                 scope_chain: list[str] | None = None,
                 config: MemoryConfig | None = None,
                 strict_scopes: bool = False,
                 scope_reads: bool = True) -> None:
        self._store = MemoryStore(db_path)
        try:
            self._scope = default_scope
            self._chain = scope_chain or []
            self._config = config
            self._allowed_scopes = self._compute_allowed_scopes()
            # Only enforce for agents with chain > 1 (skip orchestrator/tier0)
            self._enforce = len(self._chain) > 1
            self._scope_reads = scope_reads
            # Validate scope against known scopes at init time
            if strict_scopes and config:
                known = config.known_scopes()
                if self._scope not in known:
                    raise ValueError(
                        f"Scope {self._scope!r} not in known scopes. "
                        f"Check memory.yaml hierarchy or AGENT_RECALL_SLUG."
                    )
        except Exception:
            self._store.close()
            raise

    def _compute_allowed_scopes(self) -> set[str]:
        allowed = set(self._chain)
        if self._chain:
            local = self._chain[-1]
            # Children from config hierarchy
            if self._config:
                allowed |= self._config.scope_children(local)
        return allowed

    def _entity_writable(self, entity_id: int) -> tuple[bool, str]:
        """Check if this agent can write to the entity. Returns (ok, reason)."""
        if not self._enforce:
            return True, ""

        specific = self._store.get_entity_scopes(entity_id)

        if not specific:
            return True, ""  # Global-only or new entity — anyone can write

        agent_specific = self._allowed_scopes - {"global"}
        if specific & agent_specific:
            return True, ""  # Overlap — entity is in agent's tree

        entity = self._store.get_entity(entity_id)
        name = entity["name"] if entity else f"id={entity_id}"
        return False, (
            f"Write blocked: Entity '{name}' belongs to scope(s) {specific}, "
            f"but your allowed scopes are {agent_specific}."
        )

    def create_entities(self, entities: list[dict]) -> dict:
        """Create or update entities with observations.

        Returns {created: int, updated: int, blocked: list}.
        'created' = new entities, 'updated' = existing entities that got new observations.
        """
        blocked: list[str] = []
        if len(entities) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(entities)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            entities = entities[:self.MAX_ITEMS_PER_CALL]
        created = 0
        updated = 0
        for e in entities:
            if not isinstance(e, dict) or "name" not in e:
                blocked.append("Invalid entity: missing 'name' field")
                continue
            name = e["name"]
            if not isinstance(name, str) or not name.strip():
                blocked.append("Invalid entity: 'name' must be a non-empty string")
                continue
            if len(name) > self.MAX_ENTITY_NAME:
                blocked.append(f"Entity name too long ({len(name)} > {self.MAX_ENTITY_NAME}): '{name[:50]}...'")
                continue
            etype = e.get("entityType", "entity")
            if len(etype) > self.MAX_ENTITY_TYPE:
                etype = etype[:self.MAX_ENTITY_TYPE]
            existing_id = self._store.find_entity(name, etype)
            if existing_id is not None:
                ok, reason = self._entity_writable(existing_id)
                if not ok:
                    blocked.append(reason)
                    continue
            entity_id = self._store.resolve_entity(name, etype)
            for obs in e.get("observations", []):
                if not isinstance(obs, str):
                    continue
                if len(obs) > self.MAX_OBSERVATION_TEXT:
                    blocked.append(f"Observation too long ({len(obs)} > {self.MAX_OBSERVATION_TEXT}) for '{name}'")
                    continue
                self._store.add_observation(entity_id, obs, scope=self._scope)
            if existing_id is not None:
                updated += 1
            else:
                created += 1
        return {"created": created, "updated": updated, "blocked": blocked}

    def create_relations(self, relations: list[dict]) -> dict:
        """Create directed relations between entities. Returns {created: int, blocked: list}."""
        blocked: list[str] = []
        if len(relations) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(relations)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            relations = relations[:self.MAX_ITEMS_PER_CALL]
        created = 0
        for r in relations:
            if not isinstance(r, dict):
                blocked.append("Invalid relation: expected a dict")
                continue
            missing = [k for k in ("from", "to", "relationType") if k not in r]
            if missing:
                blocked.append(f"Invalid relation: missing fields {missing}")
                continue
            rel_type = r.get("relationType", "")
            if len(str(rel_type)) > self.MAX_RELATION_TYPE:
                blocked.append(f"Relation type too long ({len(str(rel_type))} > {self.MAX_RELATION_TYPE})")
                continue
            from_id = self._store.find_entity(r["from"])
            to_id = self._store.find_entity(r["to"])
            if self._enforce and from_id is not None and to_id is not None:
                from_ok = self._entity_writable(from_id)[0]
                if not from_ok:
                    blocked.append(
                        f"Blocked: '{r['from']}' is not in your allowed scopes. "
                        f"You can only create relations from entities you own."
                    )
                    continue
            if from_id is None:
                from_id = self._store.resolve_entity(r["from"], "entity")
            if to_id is None:
                to_id = self._store.resolve_entity(r["to"], "entity")
            self._store.add_relation(from_id, to_id, r["relationType"],
                                     scope=self._scope)
            created += 1
        return {"created": created, "blocked": blocked}

    def add_observations(self, observations: list[dict]) -> dict:
        """Add observations to existing entities. Returns {added: int, blocked: list}."""
        blocked: list[str] = []
        if len(observations) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(observations)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            observations = observations[:self.MAX_ITEMS_PER_CALL]
        added = 0
        for item in observations:
            if not isinstance(item, dict):
                blocked.append("Invalid observation: expected a dict")
                continue
            if "entityName" not in item or "contents" not in item:
                blocked.append("Invalid observation: missing 'entityName' or 'contents'")
                continue
            if not isinstance(item["contents"], list):
                blocked.append(f"Invalid observation for '{item['entityName']}': 'contents' must be a list")
                continue
            entity_id = self._store.find_entity(item["entityName"])
            if entity_id is None:
                blocked.append(f"Entity not found: '{item['entityName']}'")
                continue
            ok, reason = self._entity_writable(entity_id)
            if not ok:
                blocked.append(reason)
                continue
            for content in item["contents"]:
                if not isinstance(content, str):
                    blocked.append(f"Invalid observation type for '{item['entityName']}': expected string")
                    continue
                if len(content) > self.MAX_OBSERVATION_TEXT:
                    blocked.append(f"Observation too long ({len(content)} > {self.MAX_OBSERVATION_TEXT}) for '{item['entityName']}'")
                    continue
                self._store.add_observation(entity_id, content, scope=self._scope)
                added += 1
        return {"added": added, "blocked": blocked}

    def delete_entities(self, names: list[str]) -> dict:
        """Delete entities by name. Returns {deleted: int, blocked: list}."""
        blocked: list[str] = []
        if len(names) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(names)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            names = names[:self.MAX_ITEMS_PER_CALL]
        deleted = 0
        for name in names:
            entity_id = self._store.find_entity(name)
            if entity_id is None:
                blocked.append(f"Entity not found: '{name}'")
                continue
            ok, reason = self._entity_writable(entity_id)
            if not ok:
                blocked.append(reason)
                continue
            self._store.delete_entity(entity_id)
            deleted += 1
        return {"deleted": deleted, "blocked": blocked}

    def delete_relations(self, relations: list[dict]) -> dict:
        """Archive relations between entities. Returns {deleted: int, blocked: list}."""
        blocked: list[str] = []
        if len(relations) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(relations)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            relations = relations[:self.MAX_ITEMS_PER_CALL]
        deleted = 0
        for r in relations:
            if not isinstance(r, dict):
                blocked.append("Invalid relation: expected a dict")
                continue
            missing = [k for k in ("from", "to", "relationType") if k not in r]
            if missing:
                blocked.append(f"Invalid relation: missing fields {missing}")
                continue
            from_id = self._store.find_entity(r["from"])
            to_id = self._store.find_entity(r["to"])
            if from_id is None or to_id is None:
                missing = []
                if from_id is None:
                    missing.append(r["from"])
                if to_id is None:
                    missing.append(r["to"])
                blocked.append(f"Entity not found: {', '.join(repr(m) for m in missing)}")
                continue
            if self._enforce:
                from_ok = self._entity_writable(from_id)[0]
                if not from_ok:
                    blocked.append(
                        f"Blocked: Cannot delete relation "
                        f"'{r['from']}' -> '{r['to']}' — source entity "
                        f"is not in your scope tree."
                    )
                    continue
            for rel in self._store.get_relations(from_id):
                if rel["to_id"] == to_id and rel["type"] == r["relationType"]:
                    self._store.archive_relation(rel["id"])
                    deleted += 1
        return {"deleted": deleted, "blocked": blocked}

    def delete_observations(self, deletions: list[dict]) -> dict:
        """Archive observations by text match. Returns {deleted: int, blocked: list}."""
        blocked: list[str] = []
        if len(deletions) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(deletions)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            deletions = deletions[:self.MAX_ITEMS_PER_CALL]
        deleted = 0
        for item in deletions:
            if not isinstance(item, dict) or "entityName" not in item or "observations" not in item:
                blocked.append("Invalid deletion: missing 'entityName' or 'observations'")
                continue
            entity_id = self._store.find_entity(item["entityName"])
            if entity_id is None:
                blocked.append(f"Entity not found: '{item['entityName']}'")
                continue
            ok, reason = self._entity_writable(entity_id)
            if not ok:
                blocked.append(reason)
                continue
            for obs_text in item["observations"]:
                deleted += self._store.delete_observation_by_text(entity_id, obs_text)
        return {"deleted": deleted, "blocked": blocked}

    def _read_scope_set(self) -> set[str] | None:
        """Return the set of scopes this agent can read, or None for unrestricted."""
        if self._enforce and self._scope_reads:
            return self._allowed_scopes | set(self._chain)
        return None

    def _entity_visible(self, entity_id: int, scope_set: set[str]) -> bool:
        """Check if entity is visible to this agent given scope_set."""
        entity_scopes = self._store.get_entity_scopes(entity_id)
        if not entity_scopes:
            return True  # Global-only entity — visible to everyone
        return bool(entity_scopes & scope_set)

    def _filter_observations(self, obs: list[dict],
                             scope_set: set[str] | None) -> list[str]:
        """Filter observation texts by scope, cap at 20."""
        if scope_set:
            return [o["text"] for o in obs if o.get("scope") in scope_set][:20]
        return [o["text"] for o in obs[:20]]

    def open_nodes(self, names: list[str]) -> list[dict]:
        """Get detailed info for entities by name. Returns list of {name, entityType, observations}."""
        scope_set = self._read_scope_set()
        results = []
        for name in names:
            entity_id = self._store.find_entity(name)
            if entity_id is None:
                continue
            if scope_set and not self._entity_visible(entity_id, scope_set):
                continue
            entity = self._store.get_entity(entity_id)
            obs = self._store.get_observations(entity_id)
            results.append({
                "name": entity["name"],
                "entityType": entity["type"],
                "observations": self._filter_observations(obs, scope_set),
            })
        return results

    def search_nodes(self, query: str, limit: int = 10) -> list[dict]:
        """Search entities by name, slot values, or observation text.

        Results are filtered by scope chain when ``scope_reads`` is True
        (default). Only entities with data in accessible scopes are returned.
        Agents without scope enforcement (tier 0, single-scope) or with
        ``scope_reads=False`` see all entities. Observations are capped at
        20 per entity to prevent context bloat.
        """
        # Cap inputs to prevent DoS
        MAX_QUERY_LENGTH = 500
        MAX_LIMIT = 100
        MAX_WORDS = 20

        query = query[:MAX_QUERY_LENGTH]
        limit = min(limit, MAX_LIMIT)

        # Cap word count for search
        words = query.split()
        if len(words) > MAX_WORDS:
            query = " ".join(words[:MAX_WORDS])

        scope_set = self._read_scope_set()

        # Over-fetch to account for scope filtering
        found = self._store.search(query, limit=limit * 5 if scope_set else limit)
        results = []
        for f in found:
            if scope_set and not self._entity_visible(f["id"], scope_set):
                continue
            obs = self._store.get_observations(f["id"])
            results.append({
                "name": f["name"],
                "entityType": f["type"],
                "observations": self._filter_observations(obs, scope_set),
            })
            if len(results) >= limit:
                break
        return results

    def read_graph(self, limit: int = 1000) -> dict:
        """Read knowledge graph, filtered by scope. Returns {entities: [...], relations: [...]}."""
        scope_set = self._read_scope_set()
        entities = []
        all_relations = []
        seen_rels: set[tuple[str, str, str]] = set()
        source = self._store.list_entities()
        for e in source:
            if scope_set and not self._entity_visible(e["id"], scope_set):
                continue
            obs = self._store.get_observations(e["id"])
            entities.append({
                "name": e["name"],
                "entityType": e["type"],
                "observations": self._filter_observations(obs, scope_set),
            })
            for r in self._store.get_relations(e["id"]):
                rel_key = (e["name"], r["to_name"], r["type"])
                if rel_key not in seen_rels:
                    seen_rels.add(rel_key)
                    all_relations.append({
                        "from": e["name"],
                        "to": r["to_name"],
                        "relationType": r["type"],
                    })
            if len(entities) >= limit:
                break
        if len(all_relations) > self.MAX_RELATIONS:
            all_relations = all_relations[:self.MAX_RELATIONS]
        return {"entities": entities, "relations": all_relations}

    def __enter__(self) -> "MCPBridge":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""
        self._store.close()
