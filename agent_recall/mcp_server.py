#!/usr/bin/env python3
"""MCP server for agent memory — drop-in for any MCP client.

Exposes tools: create_entities, create_relations, add_observations,
delete_entities, delete_relations, delete_observations, read_graph, search_nodes, open_nodes.

Usage in MCP config:
    "command": "python3",
    "args": ["-m", "agent_recall.mcp_server"]
"""
import atexit
import json
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise ImportError(
        "MCP server requires the 'mcp' package. "
        "Install with: pip install 'agent-recall[mcp]'"
    ) from e

from agent_recall.mcp_bridge import MCPBridge
from agent_recall.config import load_config

# Singleton bridge — safe for MCP stdio (single-threaded). Not thread-safe.
bridge: MCPBridge | None = None


def _cleanup_bridge() -> None:
    if bridge is not None:
        bridge.close()


atexit.register(_cleanup_bridge)


def _bridge() -> MCPBridge:
    global bridge
    if bridge is None:
        config = load_config()
        from agent_recall.context_gen.cache import _sanitize_slug
        slug = _sanitize_slug(os.environ.get("AGENT_RECALL_SLUG") or Path.cwd().name)
        agent = config.get_agent(slug)
        scope = agent.chain[-1] if agent.chain else "global"
        scope_reads = agent.agent_type != "orchestrator"
        bridge = MCPBridge(config.db_path, default_scope=scope,
                           scope_chain=agent.chain, config=config,
                           scope_reads=scope_reads)
    return bridge


mcp = FastMCP(
    "memory",
    instructions=(
        "You have a persistent knowledge graph that survives across sessions. "
        "PROACTIVELY save important information as you encounter it during conversation — "
        "don't wait to be asked.\n\n"
        "Save:\n"
        "- People: names, roles, contact info, preferences, communication style\n"
        "- Decisions: technical choices, agreements, rationale\n"
        "- Facts: project status, deadlines, blockers, dependencies\n"
        "- Context: meeting outcomes, requirements, priorities\n\n"
        "Before creating an entity, use search_nodes to check if it already exists. "
        "Add observations to existing entities rather than creating duplicates.\n"
        "Don't save trivial or ephemeral information (typos, one-off debug values, etc.)."
    ),
)

# ponytail: warm embedding model on startup so first tool call doesn't timeout
try:
    from agent_recall.embeddings import get_provider as _get_provider
    _emb = _get_provider()
    if _emb:
        _emb.embed("warmup")
except Exception:
    pass


@mcp.tool()
def create_entities(entities: list[dict]) -> str:
    """Create new entities (people, projects, tools, concepts) in the knowledge graph.

    Use when you encounter someone or something worth remembering across sessions.
    Always search_nodes first to avoid duplicates.

    Each entity: {"name": "Alice", "entityType": "person", "observations": ["Lead engineer at Acme"]}
    """
    return json.dumps(_bridge().create_entities(entities))


@mcp.tool()
def create_relations(relations: list[dict]) -> str:
    """Link entities together in the knowledge graph.

    Use when you discover relationships: works_at, manages, depends_on, contact_for, etc.

    Each relation: {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    """
    return json.dumps(_bridge().create_relations(relations))


@mcp.tool()
def add_observations(observations: list[dict]) -> str:
    """Add new facts to existing entities. This is the most common write operation.

    Use when you learn something new about a known person, project, or concept —
    roles, decisions, preferences, status changes, meeting outcomes.

    Each observation: {"entityName": "Alice", "contents": ["Prefers async communication"]}
    """
    return json.dumps(_bridge().add_observations(observations))


@mcp.tool()
def delete_entities(entityNames: list[str]) -> str:
    """Delete entities and their relations from the knowledge graph."""
    return json.dumps(_bridge().delete_entities(entityNames))


@mcp.tool()
def delete_relations(relations: list[dict]) -> str:
    """Delete relations from the knowledge graph."""
    return json.dumps(_bridge().delete_relations(relations))


@mcp.tool()
def delete_observations(deletions: list[dict]) -> str:
    """Delete specific observations from entities."""
    return json.dumps(_bridge().delete_observations(deletions))


@mcp.tool()
def read_graph(limit: int = 1000) -> str:
    """Read the full knowledge graph. Use sparingly — prefer search_nodes for targeted lookups."""
    return json.dumps(_bridge().read_graph(limit=limit), ensure_ascii=False)


@mcp.tool()
def search_nodes(query: str, limit: int = 10) -> str:
    """Search the knowledge graph by name or content.

    Use BEFORE creating entities to check for duplicates.
    Also use to recall information about people, projects, or decisions.

    Returns up to `limit` results (default 10), filtered by scope.
    """
    return json.dumps(_bridge().search_nodes(query, limit=limit), ensure_ascii=False)


@mcp.tool()
def open_nodes(names: list[str]) -> str:
    """Retrieve full details for specific entities by name.

    Use when you need complete context about known people, projects, or concepts.
    """
    return json.dumps(_bridge().open_nodes(names), ensure_ascii=False)


# ══════════════════════════════════════════════
# v0.5.0: Extended tools
# ══════════════════════════════════════════════

@mcp.tool()
def vector_search(query: str, limit: int = 10,
                  min_score: float = 0.5) -> str:
    """Semantic vector search across observations. Falls back to FTS5 if no embedding provider.

    Use when you need to find information by meaning, not exact keywords.
    Results include similarity scores.
    """
    return _bridge().vector_search(query, limit=limit, min_score=min_score)


@mcp.tool()
def timeline(entity_name: str | None = None,
             entity_type: str | None = None,
             since: str | None = None,
             until: str | None = None,
             limit: int = 20) -> str:
    """Get a chronological timeline of observations.

    Filter by entity name, type, or date range.
    Use when you need to understand what happened and when.
    """
    return _bridge().timeline(
        entity_name=entity_name, entity_type=entity_type,
        since=since, until=until, limit=limit,
    )


@mcp.tool()
def get_context(layer: str = "compact",
                include_entities: list[str] | None = None,
                since: str | None = None) -> str:
    """Get context at different detail levels.

    compact (~500 tokens): entity names + latest observation each.
    timeline (~3000 tokens): observations in chronological order.
    full (~8000 tokens): all entities with slots and observations.
    Use this to restore context after compaction or at session start.
    """
    return _bridge().get_context(
        layer=layer, include_entities=include_entities, since=since,
    )


@mcp.tool()
def set_budget(scope: str, budget_tokens: int) -> str:
    """Set the token budget for a scope. Controls how much context is returned.

    Use when you need to limit or increase memory output for a specific scope.
    """
    return _bridge().set_budget(scope, budget_tokens)


@mcp.tool()
def promote_knowledge(observation_id: int, target_tier: str) -> str:
    """Promote an observation to a higher knowledge tier (hot/warm/cold).

    Hot tier items are auto-injected at session start.
    Use for important facts you want to keep readily available.
    """
    return _bridge().promote_knowledge(observation_id, target_tier)


@mcp.tool()
def synthesize(since_days: int = 7) -> str:
    """Run cross-source synthesis on recent observations.

    Discovers themes, patterns, and contradictions across entities.
    Use periodically to surface insights from accumulated knowledge.
    """
    return _bridge().synthesize(since_days=since_days)


@mcp.tool()
def hot_cache_status() -> str:
    """Show the current hot cache status — which memories are auto-injected.

    Returns tier distribution and top hot items for the current scope.
    """
    return _bridge().hot_cache_status()


@mcp.tool()
def adjust_trust(memory_id: int, reason: str,
                 note: str | None = None) -> str:
    """Adjust the trust score for a specific memory.

    Valid reasons: used_correctly, explicitly_confirmed, cross_validated,
    outdated, partially_incorrect, factually_wrong, superseded,
    low_utility, contradiction_resolved.
    Use when you discover a memory is more or less reliable than initially thought.
    """
    return _bridge().adjust_trust(memory_id, reason, note)


# ---------------------------------------------------------------------------
# OMC (Online Memory & Cognition) tools — P0 self-evolution system
# ---------------------------------------------------------------------------


@mcp.tool()
def omc_wake_up() -> str:
    """Generate L0+L1 wake-up context for session priming.

    Returns identity (L0) + hot facts (L1) — under 200 tokens.
    Use to test what context a new session would receive.

    Auto-initializes agent_role identity if missing.
    """
    bridge = _bridge()
    # Ensure agent_role exists
    bridge.omc_init_agent_role()
    return bridge.omc_wake_up()


@mcp.tool()
def omc_init_agent_role(role_text: str | None = None) -> str:
    """Initialize the agent_role identity entity for wake_up priming.

    Idempotent — safe to call multiple times.
    Must be called once before omc_wake_up returns useful content.

    Args:
        role_text: Optional custom role description. Uses default if omitted.
    """
    result = _bridge().omc_init_agent_role(role_text)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def omc_dedup(threshold: float = 0.85, dry_run: bool = False) -> str:
    """Run semantic deduplication on observations.

    Finds near-duplicate observations using cosine similarity
    and merges them. Falls back to FTS5 keyword matching when
    embedding provider is unavailable.

    Args:
        threshold: Cosine similarity threshold (0.0-1.0). Default 0.85.
        dry_run: If true, preview only, no changes.
    """
    import os
    from agent_recall.store import MemoryStore
    from agent_recall.omc import dedup_observations

    db_path = os.path.expanduser("~/.agent-recall/frames.db")
    store = MemoryStore(db_path)
    try:
        result = dedup_observations(store, threshold=threshold, dry_run=dry_run)
        return json.dumps(result, indent=2, ensure_ascii=False)
    finally:
        store.close()


@mcp.tool()
def omc_cleanup() -> str:
    """Run comprehensive cleanup: expired observations, cold data,
    orphan entities, old retrieval logs, zero-match patterns.
    """
    from agent_recall.omc import run_maintenance
    result = run_maintenance()
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_skill_scan() -> str:
    """Discover and register all skills from .claude/skills/,
    plugins cache, and user skills directory.

    Creates/updates skill entities in the knowledge graph.
    Returns list of discovered skills with their status.
    """
    from agent_recall.omc import discover_skills, register_skills

    discovered = discover_skills()
    registration = register_skills(discovered)
    return json.dumps({
        "discovered": len(discovered),
        "registration": registration,
        "skills": discovered,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_skill_prune(dry_run: bool = True) -> str:
    """Prune unused/deprecated skills to reduce token consumption.

    Rules:
    - 30+ days unused → disable in settings.json (zero token)
    - 3 consecutive rejected edit epochs → deprecate
    - 90 days disabled → archive + delete

    Uses EXACT matching on plugin key name (before @).

    Args:
        dry_run: If true (default), preview only. Set false to execute.
    """
    from agent_recall.omc import get_skills_to_prune, prune_skills_in_settings

    candidates = get_skills_to_prune()
    result = prune_skills_in_settings(candidates, dry_run=dry_run)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_edit_create(
    op: str,
    content: str,
    target: str,
    source_type: str = "pattern",
    merge_level: str = "overwrite",
) -> str:
    """Create an edit candidate for skill evolution (SkillOpt ReflACT pipeline).

    Args:
        op: One of append, insert_after, replace, delete
        content: The new content to add/replace with
        target: Target file path or section name
        source_type: correction, preference, pattern, bugfix
        merge_level: overwrite, merge_concat, merge_summarize
    """
    from datetime import datetime, timezone
    import os
    from agent_recall.store import MemoryStore

    db_path = os.path.expanduser("~/.agent-recall/frames.db")
    store = MemoryStore(db_path)
    try:
        conn = store._conn
        now = datetime.now(timezone.utc).isoformat()
        session_id = os.environ.get("AGENT_RECALL_SLUG", "unknown")

        conn.execute(
            """INSERT INTO edits (op, content, target, source_type, merge_level,
               update_origin, update_target, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?)""",
            (op, content, target, source_type, merge_level, session_id, target, now),
        )
        edit_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        return json.dumps({
            "edit_id": edit_id,
            "op": op,
            "target": target,
            "status": "candidate",
        }, ensure_ascii=False)
    finally:
        store.close()


@mcp.tool()
def omc_edit_list(status: str = "candidate", limit: int = 20) -> str:
    """List edit candidates by status.

    Args:
        status: candidate, selected, applied, validated, rejected
        limit: Max number of results
    """
    import os
    from agent_recall.store import MemoryStore

    db_path = os.path.expanduser("~/.agent-recall/frames.db")
    store = MemoryStore(db_path)
    try:
        conn = store._conn
        rows = conn.execute(
            """SELECT id, op, content, target, support_count, source_type,
                      merge_level, status, created_at
               FROM edits WHERE status = ? ORDER BY support_count DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
        return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)
    finally:
        store.close()


@mcp.tool()
def omc_edit_apply(edit_ids: list[int], dry_run: bool = True) -> str:
    """Apply selected edits to target skill files.

    Respects protected regions (OMC:SLOW_UPDATE_START/END markers).
    Rejected edits are rolled back.

    Args:
        edit_ids: List of edit IDs to apply
        dry_run: If true, preview changes without writing
    """
    import os
    from pathlib import Path
    from agent_recall.store import MemoryStore

    db_path = os.path.expanduser("~/.agent-recall/frames.db")
    store = MemoryStore(db_path)
    results = []
    try:
        conn = store._conn
        for eid in edit_ids:
            edit = conn.execute(
                "SELECT * FROM edits WHERE id = ? AND status = 'candidate'", (eid,)
            ).fetchone()
            if not edit:
                results.append({"edit_id": eid, "error": "Not found or not candidate"})
                continue

            target_path = Path(edit["target"])
            if not target_path.exists():
                results.append({"edit_id": eid, "error": f"Target not found: {target_path}"})
                continue

            content = target_path.read_text(encoding="utf-8", errors="ignore")
            new_content = _apply_edit_op(content, edit["op"], edit["content"], edit["target"])

            if new_content is None:
                results.append({"edit_id": eid, "error": "Protected region violation"})
                continue

            if not dry_run:
                # Backup
                backup_path = target_path.with_suffix(target_path.suffix + ".omc-bak")
                backup_path.write_text(content, encoding="utf-8")
                # Apply
                target_path.write_text(new_content, encoding="utf-8")
                conn.execute(
                    "UPDATE edits SET status='applied', applied_at=datetime('now') WHERE id=?",
                    (eid,),
                )
                conn.commit()

            results.append({
                "edit_id": eid,
                "op": edit["op"],
                "target": str(target_path),
                "status": "applied" if not dry_run else "preview",
            })

        return json.dumps(results, indent=2, ensure_ascii=False)
    finally:
        store.close()


def _apply_edit_op(content: str, op: str, edit_content: str, target: str) -> str | None:
    """Apply a single edit operation to content. Returns None if protected region hit."""
    PROTECTED_START = "<!-- OMC:SLOW_UPDATE_START -->"
    PROTECTED_END = "<!-- OMC:SLOW_UPDATE_END -->"

    if op == "append":
        # Append before any protected region
        protected_idx = content.find(PROTECTED_START)
        if protected_idx == -1:
            return content + "\n" + edit_content
        else:
            return content[:protected_idx] + edit_content + "\n" + content[protected_idx:]

    elif op == "insert_after":
        if target in content:
            idx = content.find(target) + len(target)
            return content[:idx] + "\n" + edit_content + content[idx:]
        # Fallback to append
        return content + "\n" + edit_content

    elif op == "replace":
        if target in content:
            # Check protected region
            start_idx = content.find(PROTECTED_START)
            end_idx = content.find(PROTECTED_END) + len(PROTECTED_END)
            target_idx = content.find(target)
            if start_idx != -1 and start_idx <= target_idx <= end_idx:
                return None  # Protected
            return content.replace(target, edit_content, 1)
        return content  # Target not found, no change

    elif op == "delete":
        if target in content:
            start_idx = content.find(PROTECTED_START)
            end_idx = content.find(PROTECTED_END) + len(PROTECTED_END)
            target_idx = content.find(target)
            if start_idx != -1 and start_idx <= target_idx <= end_idx:
                return None  # Protected
            return content.replace(target, "", 1)
        return content

    return content


# ---------------------------------------------------------------------------
# OMC ReflACT Epoch tools
# ---------------------------------------------------------------------------


@mcp.tool()
def omc_epoch_status() -> str:
    """Get current ReflACT pipeline status.

    Returns epoch history, edit counts by status, LR scheduler state,
    and rollout statistics.
    """
    from agent_recall.omc import get_epoch_status
    return json.dumps(get_epoch_status(), indent=2, ensure_ascii=False)


@mcp.tool()
def omc_epoch_run(dry_run: bool = True) -> str:
    """Execute one full ReflACT epoch cycle.

    Phases: Reflect → Aggregate → Select → Apply → Validate(stub)

    Auto-checks should_start_epoch() first.
    Only applies edits if conditions are met and dry_run=False.

    Args:
        dry_run: If true (default), simulate without writing files
    """
    from agent_recall.omc import should_start_epoch, run_epoch

    check = should_start_epoch()
    if not check["should_start"]:
        return json.dumps({
            "executed": False,
            "reason": check["reason"],
            "candidate_edits": check["candidate_edits"],
        }, indent=2, ensure_ascii=False)

    result = run_epoch(dry_run=dry_run)
    result["check"] = check
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def omc_epoch_check() -> str:
    """Check if conditions are met to start a new ReflACT epoch.

    Conditions (any one triggers):
    - 10+ candidate edits accumulated
    - 20+ sessions since last epoch
    - 7+ days since last epoch
    """
    from agent_recall.omc import should_start_epoch
    return json.dumps(should_start_epoch(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# P1: Validate + Slow Update + Meta Skill tools
# ---------------------------------------------------------------------------

@mcp.tool()
def omc_validate(edit_ids: list[int]) -> str:
    """Validate edits against held-out sessions.

    Compares rollout scores on held-out sessions to determine
    if edits should be accepted or rejected.

    Args:
        edit_ids: List of edit IDs to validate
    """
    from agent_recall.omc import validate_edits
    return json.dumps(validate_edits(edit_ids), indent=2, ensure_ascii=False)


@mcp.tool()
def omc_slow_update(
    skill_path: str,
    new_content: str,
    dry_run: bool = True,
) -> str:
    """Execute Slow Update on a skill's protected region.

    Only modifies content between <!-- OMC:SLOW_UPDATE_START/END --> markers.
    Backs up original before writing.

    Args:
        skill_path: Absolute path to the skill SKILL.md file
        new_content: New content for the protected region
        dry_run: If true, preview only
    """
    from agent_recall.omc import run_slow_update
    return json.dumps(
        run_slow_update(skill_path, new_content, dry_run=dry_run),
        indent=2, ensure_ascii=False,
    )


@mcp.tool()
def omc_meta_strategies(
    target_field: str | None = None,
    min_applications: int = 3,
) -> str:
    """Get best-performing meta strategies ranked by success rate.

    Args:
        target_field: Optional filter (e.g. 'edit_accept_rate')
        min_applications: Minimum times applied (default 3)
    """
    from agent_recall.omc import get_best_strategies
    return json.dumps(
        get_best_strategies(target_field, min_applications),
        indent=2, ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# P2: Multi-signal search + Spatial + LLM intent + Dashboard tools
# ---------------------------------------------------------------------------

@mcp.tool()
def omc_search(
    query: str,
    scope: str = "global",
    top_k: int = 10,
) -> str:
    """Multi-signal fusion search: semantic(0.40)+BM25(0.20)+entity(0.25)+recency(0.15).

    Returns top-k observations ranked by fused relevance score.

    Args:
        query: Natural language search query
        scope: Scope filter (default: global)
        top_k: Number of results (default: 10)
    """
    from agent_recall.omc import multi_signal_search
    results = multi_signal_search(query, scope, top_k)
    return json.dumps(results, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_spatial_map() -> str:
    """Get spatial hierarchy map (MemPalace: Wing→Room→Closet→Drawer).

    Returns nested structure of all spatially-organized entities.
    """
    from agent_recall.omc import get_spatial_map
    smap = get_spatial_map()
    # Summarize: count entities per wing
    summary = {}
    for wing, rooms in smap.items():
        total = sum(
            sum(len(items) for items in closet.values())
            for closet in rooms.values()
        )
        summary[wing] = {"rooms": len(rooms), "entities": total}
    return json.dumps({"summary": summary, "map": smap}, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_spatial_backfill(dry_run: bool = True) -> str:
    """Backfill spatial locations for unclassified entities using LLM agent.

    Scans all non-session, non-skill entities without a Spatial location
    observation, classifies them via LLM into Wing→Room→Closet, and writes
    the results.

    Args:
        dry_run: If true (default), preview only. Set false to execute.
    """
    import os as _os
    from agent_recall.store import MemoryStore
    from agent_recall.omc import create_spatial_entity

    db_path = _os.path.expanduser("~/.agent-recall/frames.db")
    store = MemoryStore(db_path)
    try:
        conn = store._conn
        # Find entities without spatial classification
        rows = conn.execute(
            """SELECT e.id, e.name, e.type,
                      GROUP_CONCAT(o.text, ' | ') as obs_text
               FROM entities e
               LEFT JOIN observations o ON e.id = o.entity_id
                   AND o.archived_at IS NULL
                   AND o.text LIKE 'Spatial location:%'
               WHERE e.type NOT IN ('session', 'skill')
                 AND o.id IS NULL
               GROUP BY e.id
               ORDER BY e.type, e.name"""
        ).fetchall()

        unclassified = []
        for r in rows:
            obs = (r["obs_text"] or "")[:300]
            unclassified.append({
                "name": r["name"],
                "type": r["type"],
                "entity_id": r["id"],
                "observations": obs,
            })

        if not unclassified:
            return json.dumps({
                "status": "complete",
                "message": "All entities already classified",
                "unclassified": 0,
            }, indent=2, ensure_ascii=False)

        if dry_run:
            return json.dumps({
                "dry_run": True,
                "unclassified": len(unclassified),
                "entities": unclassified,
                "message": f"Would classify {len(unclassified)} entities (preview)",
            }, indent=2, ensure_ascii=False)

        # Non-dry-run: this is designed to be called after an LLM agent
        # produces classifications. The agent reads the preview output
        # and calls create_spatial_entity for each.
        return json.dumps({
            "status": "ready",
            "unclassified": len(unclassified),
            "entities": unclassified,
            "instruction": "Feed these entities to an LLM agent to classify into Wing:Room:Closet, then call create_spatial_entity for each.",
        }, indent=2, ensure_ascii=False)

    finally:
        store.close()


@mcp.tool()
def omc_intent_classify(text: str, use_llm: bool = False) -> str:
    """Classify observation text into intent type.

    Args:
        text: Observation text to classify
        use_llm: If true, try LLM-based classification (falls back to keyword)

    Returns:
        intent_type, confidence, method
    """
    if use_llm:
        from agent_recall.omc import classify_intent_llm
        return json.dumps(classify_intent_llm(text), indent=2, ensure_ascii=False)
    else:
        from agent_recall.omc import classify_intent
        return json.dumps({
            "intent_type": classify_intent(text),
            "confidence": 0.5,
            "method": "keyword",
        }, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_status() -> str:
    """Get comprehensive OMC system health dashboard.

    Covers: storage, tiers, edits, epochs, skills, retrieval, patterns, meta strategies.
    Single entry point for system monitoring.
    """
    from agent_recall.omc import get_full_status
    return json.dumps(get_full_status(), indent=2, ensure_ascii=False)


@mcp.tool()
def omc_import_history(max_sessions: int = 50, dry_run: bool = True) -> str:
    """Import historical session transcripts into OMC knowledge graph.

    Scans ~/.claude/projects/ for past session JSONL files,
    extracts preferences, decisions, constraints from user messages,
    and stores them as structured observations.

    Args:
        max_sessions: Max sessions to import (default 50)
        dry_run: If true (default), preview only. Set false to execute.
    """
    from agent_recall.omc import import_transcripts
    result = import_transcripts(max_sessions=max_sessions, dry_run=dry_run)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ═══ v3.0: Hermes + mem0 parity tools ═══


@mcp.tool()
def omc_extract_facts(
    text: str,
    use_llm: bool = True,
    dry_run: bool = False,
) -> str:
    """Extract structured facts from text using LLM (mem0 1-pass compatible).

    Returns facts with memory types: event|state|plan|relationship|preference|absence|fact.
    Falls back to keyword extraction if LLM unavailable.

    Args:
        text: Conversation text to extract facts from
        use_llm: If true, use LLM extraction (falls back to keyword)
        dry_run: If true, preview only without storing
    """
    if dry_run:
        from agent_recall.omc import extract_facts_llm, _extract_facts_keyword
        facts = extract_facts_llm(text) if use_llm else _extract_facts_keyword(text)
        return json.dumps({"extracted": len(facts), "facts": facts}, indent=2, ensure_ascii=False)

    from agent_recall.omc import run_extraction_pipeline
    result = run_extraction_pipeline(text, use_llm=use_llm, dry_run=False)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_memory_types(limit: int = 500) -> str:
    """Backfill memory_type for observations without one.

    Classifies into 7 types: event, state, plan, relationship,
    preference, absence, fact.
    """
    from agent_recall.omc import apply_memory_type_metadata
    count = apply_memory_type_metadata(limit=limit)
    return json.dumps({"updated": count, "limit": limit}, ensure_ascii=False)


@mcp.tool()
def omc_multi_graph_search(
    query: str,
    top_k: int = 10,
    beam_width: int = 3,
    use_rerank: bool = False,
) -> str:
    """Multi-graph beam search: semantic + temporal + causal graphs.

    Three parallel traversals with beam pruning and fused ranking.
    Optional cross-encoder rerank for top results.

    Args:
        query: Natural language search query
        top_k: Number of results to return
        beam_width: Beam width for graph traversal
        use_rerank: Apply cross-encoder reranking to top results
    """
    from agent_recall.omc import multi_graph_search, cross_encode_rerank, classify_query_intent

    mode = classify_query_intent(query)
    results = multi_graph_search(query, top_k=top_k * 2 if use_rerank else top_k,
                                 beam_width=beam_width)
    if use_rerank and results:
        results = cross_encode_rerank(query, results, top_k=top_k)

    return json.dumps({
        "query_mode": mode,
        "results": results,
        "count": len(results),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_audit_trail(
    limit: int = 50,
    operation: str | None = None,
    session_id: str | None = None,
) -> str:
    """Read recent audit trail entries.

    Every write operation is logged to ~/.agent-recall/audit-trace.jsonl.

    Args:
        limit: Max entries to return
        operation: Optional filter by operation type
        session_id: Optional filter by session
    """
    from agent_recall.omc import read_audit_trail
    entries = read_audit_trail(limit=limit, operation=operation,
                               session_id=session_id)
    return json.dumps({
        "count": len(entries),
        "entries": entries,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def omc_distill_persona(min_confidence: float = 0.6) -> str:
    """Distill a living persona from accumulated preferences and decisions.

    NexSandglass Soul Distillation equivalent.
    Aggregates high-confidence preference/decision observations
    into a compact persona profile.
    """
    from agent_recall.omc import distill_persona
    persona = distill_persona(min_confidence=min_confidence)
    return json.dumps(persona, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
