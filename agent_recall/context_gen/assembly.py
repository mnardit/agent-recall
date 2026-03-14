"""Context assembly for orchestrator and topic agents."""
import logging

from agent_recall.store import MemoryStore
from agent_recall.context_helpers import (
    MAX_OBSERVATION_LENGTH,
    format_observation,
    format_slots,
    format_entity_header,
    format_relation,
    format_log_entry,
    apply_budget,
)

log = logging.getLogger(__name__)


def _collect_relations(store: MemoryStore,
                       entities: list[dict]) -> list[str]:
    """Collect deduplicated relation lines from a list of entities."""
    rel_lines = []
    seen_rels: set[int] = set()
    for e in entities:
        rels = store.get_relations(e["id"])
        for r in rels:
            if r["id"] not in seen_rels:
                seen_rels.add(r["id"])
                rel_lines.append(format_relation(e["name"], r))
    return rel_lines


def _collect_logs(store: MemoryStore, entities: list[dict],
                  limit_per_entity: int = 3) -> list[tuple[str, str]]:
    """Collect log entries from entities, returning (date, line) tuples for sorting."""
    log_lines: list[tuple[str, str]] = []
    for e in entities:
        logs = store.get_logs(e["id"], limit=limit_per_entity)
        for entry in logs:
            log_lines.append(format_log_entry(e["name"], entry))
    log_lines.sort(key=lambda x: x[0], reverse=True)
    return log_lines


def _format_entity_with_observations(
    name: str,
    slots: dict[str, str] | None,
    observations: list[dict],
    max_obs: int = 5,
    entity_type: str | None = None,
) -> str:
    """Format an entity with its observations as a markdown list item.

    Args:
        name: Entity name.
        slots: Slot dict (may be empty/None).
        observations: Pre-filtered list of observation dicts (must have 'text').
        max_obs: Maximum number of observations to include.
        entity_type: Optional type label (e.g. "person", "client").
    """
    header = format_entity_header(name, slots or None, entity_type=entity_type)
    line = f"- {header}"
    for o in observations[:max_obs]:
        obs_text = o['text'][:MAX_OBSERVATION_LENGTH]
        line += f"\n  - {format_observation(obs_text)}"
    return line


def _assemble_orchestrator_context(store: MemoryStore, budget: int) -> str:
    """Build comprehensive raw context for orchestrator -- ALL scopes, ALL entities."""
    sections: list[str] = []

    # People (all)
    people = store.list_entities(entity_type="person")
    if people:
        lines = []
        for p in people:
            slots = store.get_slots(p["id"])
            if not slots:
                continue
            obs = store.get_observations(p["id"])
            lines.append(_format_entity_with_observations(
                p["name"], slots, obs, max_obs=5))
        if lines:
            sections.append("## People\n" + "\n".join(lines))

    # Clients & Agencies
    for etype in ("agency", "client"):
        entities = store.list_entities(entity_type=etype)
        if entities:
            lines = []
            for e in entities:
                slots = store.get_slots(e["id"])
                s = format_slots(slots) if slots else "no data"
                lines.append(f"- **{e['name']}** ({s})")
            sections.append(f"## {etype.title()}s\n" + "\n".join(lines))

    # Projects
    projects = store.list_entities(entity_type="project")
    if projects:
        lines = []
        for p in projects:
            slots = store.get_slots(p["id"])
            s = format_slots(slots) if slots else "no data"
            lines.append(f"- **{p['name']}** ({s})")
        sections.append("## Projects\n" + "\n".join(lines))

    # Topics (open)
    topics = store.list_entities(entity_type="topic")
    if topics:
        lines = []
        for t in topics:
            slots = store.get_slots(t["id"])
            status = slots.get("status", "open")
            parent = slots.get("parent_project", "?")
            origin = slots.get("origin", "")
            icon = "\u25cf" if status == "open" else "\u25cb"
            lines.append(f"- {icon} **{t['name']}** (parent: {parent}, {status}): {origin}")
        sections.append("## Topics\n" + "\n".join(lines))

    # Key Relations
    all_entities = store.list_entities()
    rel_lines = _collect_relations(store, all_entities)
    if rel_lines:
        sections.append("## Relations\n" + "\n".join(rel_lines[:50]))

    # Recent Logs (last 20 across all entities)
    log_lines = _collect_logs(store, all_entities, limit_per_entity=3)
    if log_lines:
        sections.append("## Recent Log\n" + "\n".join(line for _, line in log_lines[:20]))

    result = "\n\n".join(sections)
    return apply_budget(result, budget)


def _assemble_topic_context(store: MemoryStore, slug: str, chain: list[str],
                            budget: int) -> str:
    """Build rich context for topic agents -- topic entity + related + scoped data."""
    sections: list[str] = []
    chain_set = set(chain)

    # Topic entity itself
    topic_eid = store.find_entity(slug, "topic")
    if topic_eid:
        slots = store.get_slots(topic_eid)
        obs = store.get_observations(topic_eid)
        lines = []
        if slots:
            lines.append("**Slots:** " + format_slots(slots))
        for o in obs:
            obs_text = o['text'][:MAX_OBSERVATION_LENGTH]
            lines.append(f"- {format_observation(obs_text)}")
        if lines:
            sections.append(f"## Topic: {slug}\n" + "\n".join(lines))

    # All entities with data in topic scope (slots OR observations)
    scoped_entities = store.list_entities_in_scopes([slug])
    obs_scoped = store.list_entities_with_observations_in_scope(slug)
    scoped_map = {e["id"]: e for e in scoped_entities}
    for e in obs_scoped:
        if e["id"] not in scoped_map:
            scoped_map[e["id"]] = e
    scoped_entities = list(scoped_map.values())

    seen_ids = {topic_eid} if topic_eid else set()
    entity_lines = []
    for e in scoped_entities:
        if e["id"] in seen_ids:
            continue
        seen_ids.add(e["id"])
        slots = store.get_slots(e["id"])
        obs = store.get_observations(e["id"])
        visible = [o for o in obs if o.get("scope") in chain_set]
        entity_lines.append(_format_entity_with_observations(
            e["name"], slots, visible, max_obs=8, entity_type=e["type"]))

    # Also pull from parent scope
    for scope in chain:
        if scope in ("global", slug):
            continue
        parent_entities = store.list_entities_in_scopes([scope])
        for e in parent_entities:
            if e["id"] in seen_ids:
                continue
            seen_ids.add(e["id"])
            slots = store.get_slots(e["id"])
            obs = store.get_observations(e["id"])
            visible = [o for o in obs if o.get("scope") in chain_set]
            entity_lines.append(_format_entity_with_observations(
                e["name"], slots, visible, max_obs=5, entity_type=e["type"]))

    if entity_lines:
        sections.append("## Related Entities\n" + "\n".join(entity_lines))

    # Relations involving topic-scoped entities
    rel_lines = _collect_relations(store, scoped_entities)
    if rel_lines:
        sections.append("## Relations\n" + "\n".join(rel_lines))

    # Logs
    all_scoped = store.list_entities_in_scopes(list(chain_set))
    log_lines = _collect_logs(store, all_scoped, limit_per_entity=5)
    if log_lines:
        sections.append("## Recent Log\n" + "\n".join(line for _, line in log_lines[:15]))

    result = "\n\n".join(sections)
    return apply_budget(result, budget)
