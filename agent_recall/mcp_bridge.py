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
            # v0.5.0: Track last accessed observation IDs for Markov transitions
            self._last_accessed: list[int] = []
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

    def _auto_link_observations(self, entity_id: int, observations: list[str]) -> int:
        """Auto-link entities mentioned in observation text. Best-effort."""
        try:
            from agent_recall.entity_linker import EntityLinker
            linker = EntityLinker(self._store)
            total = 0
            for text in observations:
                if len(text) >= 10:
                    # Find the observation we just created
                    obs_list = self._store.get_observations(entity_id)
                    for o in obs_list:
                        if o["text"] == text:
                            total += linker.link_entities_in_observation(o["id"], text)
                            break
            return total
        except Exception:
            return 0

    def create_entities(self, entities: list[dict]) -> dict:
        """Create or update entities with observations.

        Returns {created: int, updated: int, blocked: list}.
        'created' = new entities, 'updated' = existing entities that got new observations.

        v0.5.0: Auto-links mentioned entities after ALL entities are created
        (deferred to avoid missing entities not yet created).
        """
        blocked: list[str] = []
        if len(entities) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(entities)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            entities = entities[:self.MAX_ITEMS_PER_CALL]
        created = 0
        updated = 0
        # Defer entity linking until all entities exist
        pending_links: list[tuple[int, list[str]]] = []
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
            obs_texts = [o for o in e.get("observations", []) if isinstance(o, str)]
            for obs in obs_texts:
                if len(obs) > self.MAX_OBSERVATION_TEXT:
                    blocked.append(f"Observation too long ({len(obs)} > {self.MAX_OBSERVATION_TEXT}) for '{name}'")
                    continue
                self._store.add_observation(entity_id, obs, scope=self._scope)
            pending_links.append((entity_id, obs_texts))
            if existing_id is not None:
                updated += 1
            else:
                created += 1
        # v0.5.0: auto-link AFTER all entities exist
        total_linked = 0
        for entity_id, obs_texts in pending_links:
            total_linked += self._auto_link_observations(entity_id, obs_texts)
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
        """Add observations to existing entities. Returns {added: int, blocked: list}.

        v0.5.0: Auto-links mentioned entities after writing observations.
        """
        blocked: list[str] = []
        if len(observations) > self.MAX_ITEMS_PER_CALL:
            blocked.append(f"Input truncated: {len(observations)} items exceeds limit of {self.MAX_ITEMS_PER_CALL}")
            observations = observations[:self.MAX_ITEMS_PER_CALL]
        added = 0
        total_linked = 0
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
            texts = []
            for content in item["contents"]:
                if not isinstance(content, str):
                    blocked.append(f"Invalid observation type for '{item['entityName']}': expected string")
                    continue
                if len(content) > self.MAX_OBSERVATION_TEXT:
                    blocked.append(f"Observation too long ({len(content)} > {self.MAX_OBSERVATION_TEXT}) for '{item['entityName']}'")
                    continue
                self._store.add_observation(entity_id, content, scope=self._scope)
                texts.append(content)
                added += 1
            # v0.5.0: auto-link entities mentioned in observation text
            total_linked += self._auto_link_observations(entity_id, texts)
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

    def _record_access(self, obs_list: list[dict], query: str = "") -> None:
        """Best-effort: record access_count + retrieval_event + Markov transitions.

        This closes the feedback loop — every retrieval increments
        access_count (driving tier promotion), logs retrieval events
        (driving helpfulness scoring), and records access transitions
        (driving prediction). Failures are silently ignored.
        """
        current_ids = [o["id"] for o in obs_list if "id" in o]
        for o in obs_list:
            try:
                obs_id = o["id"]
                self._store.update_access(obs_id)
                if query:
                    self._store.log_retrieval(query, obs_id, similarity=0.5)
                # v0.5.0: Record Markov transitions from previously-accessed
                for prev_id in self._last_accessed:
                    if prev_id != obs_id:
                        try:
                            self._store.record_transition(prev_id, obs_id)
                        except Exception:
                            pass
            except Exception:
                pass  # Best-effort; never break the search
        # Update tracking for next call
        if current_ids:
            self._last_accessed = current_ids[:20]  # Cap at 20 to limit DB writes

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
            # v0.5.0: auto-record access for feedback loop
            self._record_access(obs, query=f"open:{name}")
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

        v0.5.0: Automatically records access_count and retrieval events
        for every observation returned, closing the feedback loop.
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
            # v0.5.0: auto-record access for feedback loop
            self._record_access(obs, query=query)
            results.append({
                "name": f["name"],
                "entityType": f["type"],
                "observations": self._filter_observations(obs, scope_set),
            })
            if len(results) >= limit:
                break
        return results

    def read_graph(self, limit: int = 1000) -> dict:
        """Read knowledge graph, filtered by scope. Returns {entities: [...], relations: [...]}.

        v0.5.0: Automatically records access_count for all returned observations.
        """
        scope_set = self._read_scope_set()
        entities = []
        all_relations = []
        seen_rels: set[tuple[str, str, str]] = set()
        source = self._store.list_entities()
        for e in source:
            if scope_set and not self._entity_visible(e["id"], scope_set):
                continue
            obs = self._store.get_observations(e["id"])
            # v0.5.0: auto-record access for feedback loop
            self._record_access(obs, query="read_graph")
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

    # ------------------------------------------------------------------
    # v0.5.0: Extended tools
    # ------------------------------------------------------------------

    def vector_search(self, query: str, limit: int = 10,
                      min_score: float = 0.5) -> str:
        """Semantic vector search (with FTS5 fallback).

        v0.5.0: Auto-records access + retrieval events + Markov transitions.
        High-similarity hits (>0.9) automatically boost trust.
        """
        import json as _json
        from agent_recall.vector_search import VectorSearchEngine
        from agent_recall.embeddings import get_provider
        provider = get_provider()
        engine = VectorSearchEngine(self._store, provider)
        try:
            results = engine.hybrid_search(query, limit=limit, scope=self._scope)
        except Exception:
            results = engine.search(query, limit=limit, min_score=min_score,
                                    scope=self._scope)
        # v0.5.0: auto-record access + retrieval + transitions + trust signals
        current_ids = []
        for r in results:
            try:
                obs_id = r.get("id")
                if obs_id:
                    current_ids.append(obs_id)
                    self._store.update_access(obs_id)
                    similarity = float(r.get("fusion_score") or r.get("similarity", 0.5))
                    self._store.log_retrieval(query, obs_id, similarity=similarity)
                    # Auto trust: high-similarity hit → +0.03
                    if similarity > 0.9:
                        try:
                            self._store.adjust_trust(
                                obs_id, "high_similarity_hit", 0.03,
                                note=f"auto: similarity={similarity:.3f}",
                            )
                        except Exception:
                            pass
            except Exception:
                pass
        # v0.5.0: Markov transitions
        for curr_id in current_ids:
            for prev_id in self._last_accessed:
                if prev_id != curr_id:
                    try:
                        self._store.record_transition(prev_id, curr_id)
                    except Exception:
                        pass
        if current_ids:
            self._last_accessed = current_ids[:20]
        return _json.dumps(results, ensure_ascii=False, default=str)

    def timeline(self, entity_name: str | None = None,
                 entity_type: str | None = None,
                 since: str | None = None,
                 until: str | None = None,
                 limit: int = 20) -> str:
        """Timeline of observations, chronologically ordered."""
        import json as _json
        results = self._store.get_timeline(
            entity_name=entity_name, entity_type=entity_type,
            scope=self._scope, since=since, until=until, limit=limit,
        )
        return _json.dumps(results, ensure_ascii=False, default=str)

    def get_context(self, layer: str = "compact",
                    include_entities: list[str] | None = None,
                    since: str | None = None) -> str:
        """Progressive disclosure: compact(~500t) | timeline(~3Kt) | full(~8Kt).

        compact: entity names + latest observation per entity
        timeline: observations in chronological order
        full: all entities with slots + observations
        """
        import json as _json
        from agent_recall.token_budget import TokenBudget, BudgetConfig
        budget = TokenBudget(self._store, self._scope)

        if layer == "compact":
            # Top entities with their latest observation
            entities = self._store.list_entities_in_scopes(
                self._chain if self._chain else [self._scope],
            )
            compact = []
            for e in entities[:20]:
                obs = self._store.get_observations(e["id"])
                latest = obs[-1]["text"] if obs else ""
                compact.append({
                    "name": e["name"],
                    "type": e["type"],
                    "latest_observation": latest[:200],
                })
            result = _json.dumps(compact, ensure_ascii=False)
            return budget.enforce("get_context_compact", result)

        elif layer == "timeline":
            results = self._store.get_timeline(
                scope=self._scope, since=since, limit=50,
            )
            result = _json.dumps(results, ensure_ascii=False, default=str)
            return budget.enforce("get_context_timeline", result)

        else:  # full
            graph = self.read_graph(limit=200)
            result = _json.dumps(graph, ensure_ascii=False)
            return budget.enforce("get_context_full", result)

    def set_budget(self, scope: str, budget_tokens: int) -> str:
        """Set token budget for a scope."""
        import json as _json
        old = self._store.get_token_budget(scope)
        self._store.set_token_budget(scope, budget_tokens)
        return _json.dumps({
            "scope": scope, "budget_tokens": budget_tokens,
            "previous": old,
        })

    def promote_knowledge(self, observation_id: int,
                          target_tier: str) -> str:
        """Manually promote an observation to a higher tier."""
        import json as _json
        try:
            from agent_recall.knowledge_tiers import KnowledgeTierManager
            mgr = KnowledgeTierManager(self._store)
            mgr.promote(observation_id, target_tier, source="manual_mcp")
            return _json.dumps({
                "observation_id": observation_id, "tier": target_tier,
                "status": "promoted",
            })
        except ValueError as e:
            return _json.dumps({"error": str(e)})

    def synthesize(self, since_days: int = 7) -> str:
        """Cross-source synthesis of recent observations."""
        import json as _json
        from agent_recall.synthesis import Synthesizer
        syn = Synthesizer(self._store)
        result = syn.synthesize(self._scope, since_days=since_days)
        return _json.dumps(result, ensure_ascii=False, default=str)

    def hot_cache_status(self) -> str:
        """Show current hot cache contents and tier distribution."""
        import json as _json
        from agent_recall.knowledge_tiers import KnowledgeTierManager
        mgr = KnowledgeTierManager(self._store)
        status = mgr.status(self._scope)
        return _json.dumps(status, ensure_ascii=False)

    def adjust_trust(self, memory_id: int, reason: str,
                     note: str | None = None) -> str:
        """Adjust trust score for an observation."""
        import json as _json
        from agent_recall.trust import TrustEngine, TrustReason
        engine = TrustEngine(self._store)
        try:
            reason_enum = TrustReason(reason)
        except ValueError:
            return _json.dumps({
                "error": f"Invalid reason '{reason}'. "
                         f"Valid: {[r.value for r in TrustReason]}",
            })
        new_trust = engine.adjust(memory_id, reason_enum, note)
        return _json.dumps({
            "memory_id": memory_id, "reason": reason,
            "new_trust": round(new_trust, 4),
        })

    def omc_wake_up(self) -> str:
        """Generate L0+L1 wake-up context for session priming.

        L0: agent identity (~50 tokens) — who the agent is.
        L1: top hot-tier facts (~150 tokens) — what the agent should remember.

        Returns JSON string with 'l0' and 'l1' keys.
        Total target: <200 tokens.
        """
        import json as _json

        conn = self._store._conn

        # L0: agent identity
        agent_row = conn.execute(
            """SELECT e.id, e.name, e.type
               FROM entities e
               WHERE e.type = 'identity' AND (e.name LIKE '%agent%' OR e.name LIKE '%role%')
               LIMIT 1"""
        ).fetchone()

        l0 = ""
        if agent_row:
            obs_rows = conn.execute(
                """SELECT o.text FROM observations o
                   WHERE o.entity_id = ? AND o.archived_at IS NULL
                   ORDER BY o.created_at DESC LIMIT 3""",
                (agent_row["id"],),
            ).fetchall()
            l0 = " ".join(r["text"][:120] for r in obs_rows if r["text"])

        if not l0:
            l0 = "资深全栈工程师 — DeepSeek V4 Pro, 中文环境, OMC self-evolution active"

        # L1: top hot-tier facts
        l1_items = []
        hot_rows = conn.execute(
            """SELECT o.text, kt.salience_score
               FROM observations o
               JOIN knowledge_tiers kt ON o.id = kt.observation_id
               WHERE kt.tier = 'hot'
                 AND o.archived_at IS NULL
               ORDER BY kt.salience_score DESC
               LIMIT 8"""
        ).fetchall()

        for r in hot_rows:
            text = (r["text"] or "")[:100]
            if text.strip():
                l1_items.append(f"- {text}")

        l1 = "\n".join(l1_items) if l1_items else "(no hot facts yet)"

        result = {
            "l0": l0[:200],
            "l1": l1[:500],
            "hot_count": len(l1_items),
        }
        return _json.dumps(result, ensure_ascii=False)

    def omc_wake_up_raw(self) -> str:
        """Return wake-up context as a plain string for injection.

        Suitable for direct injection into system prompt additionalContext.
        Target: <200 tokens (~500 chars).
        """
        import json as _json
        data = _json.loads(self.omc_wake_up())
        parts = [data["l0"]]
        if data["l1"] and data["l1"] != "(no hot facts yet)":
            parts.append(data["l1"])
        return "\n".join(parts)[:600]

    def omc_init_agent_role(self, role_text: str | None = None) -> dict:
        """Initialize the agent_role identity entity if it doesn't exist.

        This ensures wake_up has content to inject at session start.
        Safe to call multiple times — idempotent.
        """
        conn = self._store._conn

        existing = conn.execute(
            "SELECT id FROM entities WHERE type='identity' AND name='agent_role'"
        ).fetchone()

        if existing:
            return {"entity_id": existing["id"], "status": "already_exists"}

        if role_text is None:
            role_text = (
                "资深全栈工程师. DeepSeek V4 Pro model. "
                "Chinese language environment. "
                "OMC self-evolution system active. "
                "Uses ReflACT pipeline for skill evolution. "
                "Dual-track memory: verbatim drawers + structured observations."
            )

        eid = conn.execute(
            "INSERT INTO entities (name, type, created_at) VALUES ('agent_role', 'identity', datetime('now'))"
        ).lastrowid

        conn.execute(
            """INSERT INTO observations (entity_id, text, scope, created_at)
               VALUES (?, ?, 'global', datetime('now'))""",
            (eid, role_text),
        )

        # Add to observation_meta
        obs_id = conn.execute(
            "SELECT id FROM observations WHERE entity_id=? ORDER BY id DESC LIMIT 1", (eid,)
        ).fetchone()["id"]
        conn.execute(
            """INSERT OR REPLACE INTO observation_meta
               (observation_id, valid_from, confidence, intent_type)
               VALUES (?, datetime('now'), 0.95, 'identity')""",
            (obs_id,),
        )

        # Add to knowledge_tiers as hot
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_tiers
               (observation_id, tier, salience_score, base_importance)
               VALUES (?, 'hot', 0.95, 0.95)""",
            (obs_id,),
        )

        conn.commit()
        return {"entity_id": eid, "observation_id": obs_id, "status": "created"}

    def __enter__(self) -> "MCPBridge":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""
        self._store.close()
