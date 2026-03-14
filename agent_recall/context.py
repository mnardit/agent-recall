"""Raw Context Assembly — structured data extraction from frames.db (no LLM).

Builds a markdown document from database entities, slots, observations, and logs.
Sections have explicit priorities (must-have -> nice-to-have) and are budget-aware.
This is the data layer; for LLM-based summarization see context_gen/ package.
"""
from pathlib import Path

from agent_recall.store import MemoryStore
from agent_recall.hierarchy import ScopedView
from agent_recall.context_helpers import (
    MAX_OBSERVATION_LENGTH,
    format_observation,
    format_slots,
    format_entity_header,
    apply_budget,
)

# Priority tiers (lower = more important, gets budget first)
PRIORITY_MUST = 1
PRIORITY_IMPORTANT = 2
PRIORITY_USEFUL = 3
PRIORITY_NICE = 4


def _is_topic(store: MemoryStore, scope: str) -> bool:
    """Check if the given scope is a topic entity."""
    eid = store.find_entity(scope, "topic")
    return eid is not None


def _find_parent_entity(store: MemoryStore, scope_name: str) -> int | None:
    """Find entity matching a scope name (case-insensitive, typed then untyped)."""
    for etype in ("agency", "client", "project", None):
        eid = store.find_entity(scope_name, etype) if etype else store.find_entity(scope_name)
        if eid:
            return eid
    # Case-insensitive fallback (scope "acme" matches entity "Acme")
    return store.find_entity_icase(scope_name)


def assemble_context(store: MemoryStore, chain: list[str], tier: int,
                     budget: int = 10000,
                     vault_projects_dir: Path | None = None,
                     task_header: str = "## Tasks") -> str:
    """Build raw context string from frames.db for an agent.

    Assembles people, tasks, topics, clients, and logs into a prioritized
    markdown document. Higher-priority sections get budget first; lower-priority
    sections are truncated or omitted if budget is exhausted.

    Args:
        store: Open MemoryStore instance.
        chain: Agent's scope chain (e.g. ["global", "acme", "proj-a"]).
        tier: Agent tier (0=silent, 1=basic, 2=full context).
        budget: Maximum output length in characters (default 10000).
        vault_projects_dir: Optional path to Obsidian vault projects/ directory
            for loading task lists from markdown files.
        task_header: Markdown header that marks the tasks section in vault files.

    Returns:
        Formatted markdown context string, or empty string for tier 0.
    """
    if tier == 0 or not chain:
        return ""

    view = ScopedView(store, chain)
    is_topic = _is_topic(store, chain[-1])

    # Collect all sections with priorities, then add in priority order
    pending: list[tuple[int, str, str]] = []  # (priority, title, content)

    # --- People ---
    chain_set = set(chain)
    non_global_chain = chain_set - {"global"}
    deep_chain = len(chain) >= 3
    people = view.list_entities(entity_type="person")
    if people:
        primary_lines = []
        secondary_lines = []
        for p in people:
            entity = view.get_entity(p["name"])
            if entity and entity["slots"]:
                # Tier 1 short chains: only people with leaf-scope data
                if tier == 1 and non_global_chain and not deep_chain:
                    leaf = chain[-1]
                    has_leaf_slots = bool(
                        store.get_slots(p["id"], scope_chain=[leaf]))
                    has_leaf_obs = any(
                        o.get("scope") == leaf
                        for o in store.get_observations(p["id"]))
                    if not has_leaf_slots and not has_leaf_obs:
                        continue

                # Deep chains: classify into primary (leaf) vs secondary (parent)
                if tier in (1, 2) and non_global_chain and deep_chain:
                    leaf = chain[-1]
                    parent = chain[-2]
                    has_leaf_slots = bool(
                        store.get_slots(p["id"], scope_chain=[leaf]))
                    clients_val = entity["slots"].get("clients", "")
                    clients_set = (
                        {c.strip().lower() for c in clients_val.split(",")}
                        if clients_val.strip() else set()
                    )
                    is_primary = has_leaf_slots or leaf in clients_set
                    is_secondary = (
                        not is_primary and parent in clients_set
                    )
                    if not is_primary and not is_secondary:
                        continue
                    if is_secondary:
                        role = entity["slots"].get("role", "")
                        label = f"- {p['name']}"
                        if role:
                            label += f" ({role})"
                        secondary_lines.append(label)
                        continue
                # Primary person (or short chain — full detail)
                line = f"- {format_entity_header(p['name'], entity['slots'])}"
                obs = store.get_observations(p["id"])
                visible = [o["text"][:MAX_OBSERVATION_LENGTH]
                           for o in obs if o.get("scope") in chain_set]
                if visible:
                    for o in visible[:5]:
                        line += f"\n  - {format_observation(o)}"
                primary_lines.append(line)
        all_lines = primary_lines[:]
        if secondary_lines:
            all_lines.append("")
            all_lines.append("**Other team members:**")
            all_lines.extend(secondary_lines)
        if all_lines:
            prio = PRIORITY_MUST
            pending.append((prio, "People", "\n".join(all_lines)))

    # --- Current Tasks ---
    if tier >= 1 and vault_projects_dir and vault_projects_dir.exists():
        task_lines = _load_vault_tasks(chain, vault_projects_dir, task_header,
                                       max_chars=3000)
        if task_lines:
            prio = PRIORITY_MUST if is_topic else PRIORITY_IMPORTANT
            pending.append((prio, "Current Tasks", "\n".join(task_lines)))

    # --- Topics ---
    if tier >= 2:
        parent_scope = chain[-2] if is_topic and len(chain) >= 2 else chain[-1]
        all_topics = store.list_entities(entity_type="topic")
        topic_lines = []
        for t in all_topics:
            t_slots = store.get_slots(t["id"])
            if t_slots.get("parent_project") == parent_scope:
                status = t_slots.get("status", "open")
                origin = t_slots.get("origin", "")
                icon = "●" if status == "open" else "○"
                topic_lines.append(
                    f"- {icon} **{t['name']}** ({status}): {origin}")
        if topic_lines:
            prio = PRIORITY_USEFUL if is_topic else PRIORITY_IMPORTANT
            pending.append((prio, "Topics", "\n".join(topic_lines)))

    # --- Parent Context (for deep chains: describe the parent org/project) ---
    if tier >= 1 and deep_chain:
        parent_name = chain[-2]
        parent_eid = _find_parent_entity(store, parent_name)
        if parent_eid:
            parent_slots = store.get_slots(parent_eid,
                                           scope_chain=chain[:-1])
            parent_obs = store.get_observations(parent_eid)
            parent_chain_set = set(chain[:-1]) | {"global"}
            visible_obs = [o["text"][:MAX_OBSERVATION_LENGTH]
                           for o in parent_obs
                           if o.get("scope") in parent_chain_set]
            p_lines = []
            p_entity = store.get_entity(parent_eid)
            if p_entity:
                p_lines.append(format_entity_header(
                    p_entity['name'], parent_slots or None))
            for o in visible_obs[:3]:
                p_lines.append(f"- {format_observation(o)}")
            if p_lines:
                pending.append((PRIORITY_USEFUL, "Parent Context",
                                "\n".join(p_lines)))

    # --- Project context (own scope observations, tier >= 1) ---
    if tier >= 1:
        leaf = chain[-1]
        leaf_entities = store.list_entities_with_observations_in_scope(leaf)
        proj_lines = []
        for e in leaf_entities:
            if e["type"] == "person":
                continue  # people handled above
            obs = store.get_observations(e["id"])
            visible = [o["text"][:MAX_OBSERVATION_LENGTH]
                       for o in obs if o.get("scope") == leaf]
            if visible:
                slots = store.get_slots(e["id"])
                proj_lines.append(
                    f"- {format_entity_header(e['name'], slots or None)}")
                for o in visible[:15]:
                    proj_lines.append(f"  - {format_observation(o)}")
        if proj_lines:
            pending.append((PRIORITY_MUST, "Project Context",
                            "\n".join(proj_lines)))

    # --- Clients, agencies, projects ---
    if tier >= 2:
        leaf = chain[-1]
        # IDs already shown in Project Context section above
        shown_ids = {e["id"] for e in
                     store.list_entities_with_observations_in_scope(leaf)
                     } if tier >= 1 else set()
        for etype in ("client", "agency", "project"):
            entities = view.list_entities(entity_type=etype)
            if entities:
                lines = []
                for e in entities:
                    if e["id"] in shown_ids:
                        continue  # already in Project Context
                    entity = view.get_entity(e["name"])
                    if entity and entity["slots"]:
                        lines.append(
                            f"- {format_entity_header(e['name'], entity['slots'])}")
                if lines:
                    prio = PRIORITY_NICE if is_topic else PRIORITY_USEFUL
                    pending.append((prio, f"{etype.title()}s",
                                    "\n".join(lines)))

    # --- Recent logs ---
    if tier >= 2:
        # Deep chains: only logs from entities with data in the leaf scope
        if deep_chain:
            log_entities = store.list_entities_in_scopes([chain[-1]])
        else:
            log_entities = view.list_entities()
        log_lines = []
        for e in log_entities:
            eid = e.get("id") or store.find_entity(e["name"])
            if eid:
                logs = store.get_logs(eid, limit=3)
                for log in logs:
                    log_lines.append(
                        f"- [{log['date']}] {e['name']}: {log['text']}")
        if log_lines:
            prio = PRIORITY_IMPORTANT if is_topic else PRIORITY_NICE
            pending.append((prio, "Recent Log",
                            "\n".join(log_lines[-10:])))

    # --- Assemble by priority ---
    pending.sort(key=lambda x: x[0])
    sections: list[str] = []
    remaining = budget

    for _prio, title, content in pending:
        header = f"## {title}\n"
        if len(header) >= remaining:
            continue
        section = header + content
        if len(section) <= remaining:
            sections.append(section)
            remaining -= len(section)
        else:
            available = remaining - len(header) - 30
            lines = content.splitlines()
            truncated = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > available:
                    break
                truncated.append(line)
                used += len(line) + 1
            if truncated:
                omitted = len(lines) - len(truncated)
                truncated.append(f"... ({omitted} more entries omitted)")
                section = header + "\n".join(truncated)
                sections.append(section)
                remaining -= len(section)

    result = "\n\n".join(sections)
    return apply_budget(result, budget)


def _load_vault_tasks(chain: list[str], vault_projects_dir: Path,
                      task_header: str, max_chars: int = 3000) -> list[str]:
    """Load task lines from vault project files for the nearest parent scope."""
    for scope in reversed(chain):
        if scope == "global":
            continue
        project_dir = (vault_projects_dir / scope).resolve()
        if not project_dir.is_relative_to(vault_projects_dir.resolve()):
            continue  # reject path traversal attempt
        if project_dir.is_dir():
            return _extract_tasks_from_dir(project_dir, task_header, max_chars)
    return []


def _extract_tasks_from_dir(project_dir: Path, task_header: str,
                            max_chars: int) -> list[str]:
    """Extract unchecked tasks from vault project files in a directory."""
    lines: list[str] = []
    chars = 0

    for md in sorted(project_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        in_section = False
        file_tasks: list[str] = []
        for line in text.splitlines():
            if line.startswith(task_header):
                in_section = True
                continue
            if in_section and line.startswith("## "):
                break
            if in_section and (line.strip().startswith("- [ ]")
                               or line.startswith("### ")):
                file_tasks.append(line)

        if file_tasks:
            header = f"**{md.stem}:**"
            section_text = header + "\n" + "\n".join(file_tasks)
            if chars + len(section_text) > max_chars:
                break
            lines.append(header)
            lines.extend(file_tasks)
            chars += len(section_text)

    return lines
