"""MCP compatibility bridge — translates MCP memory protocol to frame store.

Maps @modelcontextprotocol/server-memory API to our SQLite store.
Observations stored in dedicated table (not as slots).
Scope enforcement: agents can only write to entities in their scope tree.
"""
from pathlib import Path

from claude_memory.config import MemoryConfig
from claude_memory.store import MemoryStore


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

    Example::

        bridge = MCPBridge("memory.db", default_scope="acme",
                           scope_chain=["global", "acme"])
        bridge.create_entities([{"name": "Alice", "entityType": "person",
                                 "observations": ["New team member"]}])
    """

    def __init__(self, db_path: Path | str, default_scope: str = "global",
                 scope_chain: list[str] | None = None,
                 config: MemoryConfig | None = None) -> None:
        self._store = MemoryStore(db_path)
        self._scope = default_scope
        self._chain = scope_chain or []
        self._config = config
        self._allowed_scopes = self._compute_allowed_scopes()
        # Only enforce for agents with chain > 1 (skip orchestrator/tier0)
        self._enforce = len(self._chain) > 1

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
            f"SCOPE VIOLATION: Entity '{name}' belongs to scope(s) {specific}, "
            f"but your allowed scopes are {agent_specific}."
        )

    def create_entities(self, entities: list[dict]) -> dict:
        """Create entities with optional observations. Returns {created: int, blocked: list}."""
        created = 0
        blocked: list[str] = []
        for e in entities:
            name = e["name"]
            etype = e.get("entityType", "entity")
            existing_id = self._store.find_entity(name, etype)
            if existing_id is not None:
                ok, reason = self._entity_writable(existing_id)
                if not ok:
                    blocked.append(reason)
                    continue
            entity_id = self._store.resolve_entity(name, etype)
            for obs in e.get("observations", []):
                self._store.add_observation(entity_id, obs, scope=self._scope)
            created += 1
        return {"created": created, "blocked": blocked}

    def create_relations(self, relations: list[dict]) -> dict:
        """Create directed relations between entities. Returns {created: int, blocked: list}."""
        created = 0
        blocked: list[str] = []
        for r in relations:
            from_id = self._store.find_entity(r["from"])
            to_id = self._store.find_entity(r["to"])
            if self._enforce and from_id is not None and to_id is not None:
                from_ok = self._entity_writable(from_id)[0]
                to_ok = self._entity_writable(to_id)[0]
                if not from_ok and not to_ok:
                    blocked.append(
                        f"SCOPE VIOLATION: Neither '{r['from']}' nor '{r['to']}' "
                        f"is in your allowed scopes."
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
        added = 0
        blocked: list[str] = []
        for item in observations:
            entity_id = self._store.find_entity(item["entityName"])
            if entity_id is None:
                continue
            ok, reason = self._entity_writable(entity_id)
            if not ok:
                blocked.append(reason)
                continue
            for content in item["contents"]:
                self._store.add_observation(entity_id, content, scope=self._scope)
                added += 1
        return {"added": added, "blocked": blocked}

    def delete_entities(self, names: list[str]) -> dict:
        """Delete entities by name. Returns {deleted: int, blocked: list}."""
        deleted = 0
        blocked: list[str] = []
        for name in names:
            entity_id = self._store.find_entity(name)
            if entity_id is None:
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
        deleted = 0
        blocked: list[str] = []
        for r in relations:
            from_id = self._store.find_entity(r["from"])
            to_id = self._store.find_entity(r["to"])
            if from_id is None or to_id is None:
                continue
            if self._enforce:
                from_ok = self._entity_writable(from_id)[0]
                to_ok = self._entity_writable(to_id)[0]
                if not from_ok and not to_ok:
                    blocked.append(
                        f"SCOPE VIOLATION: Cannot delete relation "
                        f"'{r['from']}' -> '{r['to']}' — neither entity "
                        f"is in your scope tree."
                    )
                    continue
            for rel in self._store.get_relations(from_id):
                if rel["to_id"] == to_id and rel["type"] == r["relationType"]:
                    self._store.archive_relation(rel["id"])
                    deleted += 1
        return {"deleted": deleted, "blocked": blocked}

    def delete_observations(self, deletions: list[dict]) -> dict:
        """Archive observations by text match. Returns {deleted: int, blocked: list}."""
        deleted = 0
        blocked: list[str] = []
        for item in deletions:
            entity_id = self._store.find_entity(item["entityName"])
            if entity_id is None:
                continue
            ok, reason = self._entity_writable(entity_id)
            if not ok:
                blocked.append(reason)
                continue
            for obs_text in item["observations"]:
                self._store.delete_observation_by_text(entity_id, obs_text)
                deleted += 1
        return {"deleted": deleted, "blocked": blocked}

    def open_nodes(self, names: list[str]) -> list[dict]:
        """Get detailed info for entities by name. Returns list of {name, entityType, observations}."""
        results = []
        for name in names:
            entity_id = self._store.find_entity(name)
            if entity_id is None:
                continue
            entity = self._store.get_entity(entity_id)
            obs = self._store.get_observations(entity_id)
            results.append({
                "name": entity["name"],
                "entityType": entity["type"],
                "observations": [o["text"] for o in obs],
            })
        return results

    def search_nodes(self, query: str) -> list[dict]:
        """Search entities by name, slot values, or observation text."""
        found = self._store.search(query)
        results = []
        for f in found:
            obs = self._store.get_observations(f["id"])
            results.append({
                "name": f["name"],
                "entityType": f["type"],
                "observations": [o["text"] for o in obs],
            })
        return results

    def read_graph(self) -> dict:
        """Read entire knowledge graph. Returns {entities: [...], relations: [...]}."""
        entities = []
        all_relations = []
        for e in self._store.list_entities():
            obs = self._store.get_observations(e["id"])
            entities.append({
                "name": e["name"],
                "entityType": e["type"],
                "observations": [o["text"] for o in obs],
            })
            for r in self._store.get_relations(e["id"]):
                all_relations.append({
                    "from": e["name"],
                    "to": r["to_name"],
                    "relationType": r["type"],
                })
        return {"entities": entities, "relations": all_relations}

    def close(self) -> None:
        """Close the underlying database connection."""
        self._store.close()
