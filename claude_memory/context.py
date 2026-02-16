"""Layered Context Assembly — build additionalContext string from frames.db.

Sections have explicit priorities (must-have -> nice-to-have).
Priorities differ for project agents vs topic agents.
Oversized sections are truncated by entries, never silently dropped.
"""
from pathlib import Path

from claude_memory.store import MemoryStore
from claude_memory.hierarchy import ScopedView

# Priority tiers (lower = more important, gets budget first)
PRIORITY_MUST = 1
PRIORITY_IMPORTANT = 2
PRIORITY_USEFUL = 3
PRIORITY_NICE = 4


def _is_topic(store: MemoryStore, scope: str) -> bool:
    """Check if the given scope is a topic entity."""
    eid = store.find_entity(scope, "topic")
    return eid is not None


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
    people = view.list_entities(entity_type="person")
    if people:
        lines = []
        for p in people:
            entity = view.get_entity(p["name"])
            if entity and entity["slots"]:
                # Skip people with no connection to leaf scope
                if tier in (1, 2) and non_global_chain and len(chain) > 2:
                    leaf = chain[-1]
                    has_leaf_slots = bool(
                        store.get_slots(p["id"], scope_chain=[leaf]))
                    if not has_leaf_slots:
                        clients_val = entity["slots"].get("clients", "")
                        if not clients_val.strip():
                            continue
                        if not any(c in clients_val for c in non_global_chain):
                            continue
                s = ", ".join(f"{k}: {v}" for k, v in entity["slots"].items())
                line = f"- **{p['name']}** ({s})"
                obs = store.get_observations(p["id"])
                visible = [o["text"] for o in obs if o.get("scope") in chain_set]
                if visible:
                    for o in visible[:5]:
                        line += f"\n  - {o}"
                lines.append(line)
        if lines:
            prio = PRIORITY_MUST
            pending.append((prio, "People", "\n".join(lines)))

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

    # --- Clients, agencies, projects ---
    if tier >= 2:
        for etype in ("client", "agency", "project"):
            entities = view.list_entities(entity_type=etype)
            if entities:
                lines = []
                for e in entities:
                    entity = view.get_entity(e["name"])
                    if entity and entity["slots"]:
                        s = ", ".join(f"{k}: {v}"
                                      for k, v in entity["slots"].items())
                        lines.append(f"- **{e['name']}** ({s})")
                if lines:
                    prio = PRIORITY_NICE if is_topic else PRIORITY_USEFUL
                    pending.append((prio, f"{etype.title()}s",
                                    "\n".join(lines)))

    # --- Recent logs ---
    if tier >= 2:
        all_entities = view.list_entities()
        log_lines = []
        for e in all_entities:
            eid = store.find_entity(e["name"])
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
    return result[:budget] if len(result) > budget else result


def _load_vault_tasks(chain: list[str], vault_projects_dir: Path,
                      task_header: str, max_chars: int = 3000) -> list[str]:
    """Load task lines from vault project files for the nearest parent scope."""
    for scope in reversed(chain):
        if scope == "global":
            continue
        project_dir = vault_projects_dir / scope
        if project_dir.is_dir():
            return _extract_tasks_from_dir(project_dir, task_header, max_chars)
    return []


def _extract_tasks_from_dir(project_dir: Path, task_header: str,
                            max_chars: int) -> list[str]:
    """Extract unchecked tasks from vault project files in a directory."""
    lines: list[str] = []
    chars = 0

    for md in sorted(project_dir.glob("*.md")):
        text = md.read_text()
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
