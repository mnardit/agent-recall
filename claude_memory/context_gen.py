"""AI Context Generation — LLM summarizes raw context into agent briefings.

Generates cached briefings per agent. SessionStart hook reads cache first,
falls back to raw context if cache is stale or missing.
"""
import json as _json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from claude_memory.config import MemoryConfig, AgentConfig, load_config
from claude_memory.context import assemble_context
from claude_memory.store import MemoryStore

log = logging.getLogger(__name__)

# Defaults (overridable via config.briefing)
DEFAULT_CACHE_MAX_AGE = 86400  # 24 hours
DEFAULT_RAW_BUDGET = 50000     # generous budget for raw input
DEFAULT_OUTPUT_BUDGET = 8000   # target output size
DEFAULT_MODEL = "opus"
DEFAULT_TIMEOUT = 300

AGENT_TYPES = ("client", "agency", "personal", "topic", "system", "orchestrator")

# Type for pluggable LLM invocation: (prompt, model, timeout) -> str | None | LLMResult
LLMCaller = Callable[[str, str, int], "str | None | LLMResult"]

MAX_LOG_ENTRIES = 10


@dataclass
class LLMResult:
    """Structured result from LLM invocation with optional token counts."""
    text: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


# --- Built-in Prompt Templates ---

_PROMPT_HEADER = (
    "You are generating a context briefing for an AI agent. "
    "The briefing will be injected into the agent's system prompt at startup.\n\n"
    "IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. "
    "Use markdown formatting. Maximum {budget} characters.\n\n"
)

BUILTIN_TEMPLATES: dict[str, str] = {
    "client": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — manages a client project.\n\n"
        "Raw data from knowledge base:\n{raw_context}\n\n"
        "Create a structured briefing:\n\n"
        "## Key People\n"
        "For each person: name, role, contact method, what to remember about them.\n"
        "Only people who actually work with THIS client.\n\n"
        "## Current Tasks\n"
        "Prioritize: urgent > in progress > can wait.\n"
        "Group by area if applicable.\n\n"
        "## Context\n"
        "Recent events, decisions, agreements. What the agent must know.\n\n"
        "## Relations\n"
        "Key dependencies between people and projects.\n\n"
        "DO NOT include: raw slot data, entity IDs, scope metadata, "
        "people unrelated to this client, completed tasks.\n"
        "Language: match the language of the raw data."
    ),
    "agency": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — manages an agency/organization.\n"
        "This agent oversees multiple sub-clients and coordinates team work.\n\n"
        "Raw data:\n{raw_context}\n\n"
        "Create a structured briefing:\n\n"
        "## Team\n"
        "Team members: roles, responsibilities, how to reach them.\n\n"
        "## Clients & Projects\n"
        "Active clients with current status and priorities.\n\n"
        "## Current Tasks\n"
        "Cross-client tasks and agency-level priorities.\n\n"
        "## Context\n"
        "Recent events, decisions, open questions.\n\n"
        "Language: match the raw data language."
    ),
    "personal": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — personal/side project.\n\n"
        "Raw data:\n{raw_context}\n\n"
        "Create a concise briefing:\n\n"
        "## People\n"
        "Who's involved and their roles.\n\n"
        "## Tasks\n"
        "Current tasks and priorities.\n\n"
        "## Context\n"
        "What the agent needs to know to be useful.\n\n"
        "Keep it brief — personal projects need less context than client work.\n"
        "Language: match the raw data language."
    ),
    "topic": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — focused topic/sub-session within a larger project.\n"
        "Topics are temporary workstreams that need sharp focus.\n\n"
        "Raw data:\n{raw_context}\n\n"
        "Create a focused briefing:\n\n"
        "## Goal\n"
        "What this topic is about and what needs to be done.\n\n"
        "## Tasks\n"
        "Current tasks, ordered by priority.\n\n"
        "## People\n"
        "Only people directly relevant to this topic.\n\n"
        "## Context\n"
        "Parent project context relevant to this topic.\n\n"
        "Be very focused — topics are narrow. Skip anything not directly relevant.\n"
        "Language: match the raw data language."
    ),
    "system": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — system utility/service agent.\n\n"
        "Raw data:\n{raw_context}\n\n"
        "Create a minimal briefing:\n\n"
        "## Role\n"
        "What this agent does in the system.\n\n"
        "## People\n"
        "Only people who interact with this service.\n\n"
        "## Context\n"
        "System-level context this agent needs.\n\n"
        "Keep it very short — system agents need minimal people context.\n"
        "Language: match the raw data language."
    ),
    "orchestrator": (
        _PROMPT_HEADER
        + "Agent: \"{slug}\" — orchestrator/meta-agent managing all other agents.\n"
        "This is the central coordinator of a multi-agent system.\n\n"
        "Raw data from knowledge base:\n{raw_context}\n\n"
        "Create a high-level briefing for the orchestrator:\n\n"
        "## Key People\n"
        "PRIORITIZE by operational importance:\n"
        "1. Owner — always first, with timezone and contacts\n"
        "2. Key business contacts\n"
        "3. Others — ONLY if they directly affect current active tasks\n"
        "Do NOT list people with no operational role.\n\n"
        "## Agents & Projects\n"
        "Overview of active agents/projects grouped by category. "
        "Current status and priorities.\n\n"
        "## Active Priorities\n"
        "Cross-project priorities, blockers, deadlines. What needs attention NOW.\n"
        "Group by urgency: critical > in progress > backlog.\n\n"
        "## Context\n"
        "Recent system-wide events, decisions, open questions.\n\n"
        "## Monitoring Points\n"
        "Key things to watch: services, timers, sessions, resources.\n\n"
        "The orchestrator needs a BIRD'S EYE VIEW — don't go deep into any single "
        "project, focus on what matters across the whole system.\n"
        "Language: match the raw data language."
    ),
}


# --- Template Loading ---

def load_template(agent_type: str, templates_dir: Path | None = None) -> str:
    """Load prompt template from file or fall back to builtin."""
    if templates_dir and templates_dir.is_dir():
        template_file = templates_dir / f"{agent_type}.md"
        if template_file.exists():
            return template_file.read_text()
    return BUILTIN_TEMPLATES.get(agent_type, BUILTIN_TEMPLATES["personal"])


def build_prompt(slug: str, agent_type: str, raw_context: str,
                 output_budget: int = DEFAULT_OUTPUT_BUDGET,
                 templates_dir: Path | None = None) -> str:
    """Build the full prompt for LLM from template + raw data."""
    template = load_template(agent_type, templates_dir)
    return template.format(
        slug=slug,
        raw_context=raw_context,
        budget=output_budget,
    )


# --- LLM Invocation ---

def default_llm_caller(prompt: str, model: str = DEFAULT_MODEL,
                       timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """Default LLM caller: runs `claude -p --model <model>`.

    Strips CLAUDECODE env var to allow nesting Claude CLI calls.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model],
            input=prompt,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            log.warning("LLM stderr: %s", result.stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        log.warning("LLM call timed out after %ds", timeout)
        return None
    except FileNotFoundError:
        log.error("claude CLI not found in PATH")
        return None


# --- Cache Management ---

def is_cache_fresh(slug: str, cache_dir: Path | None = None,
                   max_age: int = DEFAULT_CACHE_MAX_AGE) -> bool:
    """Check if cached briefing exists, is younger than max_age, and not stale."""
    cache_dir = cache_dir or _default_cache_dir()
    cache_path = cache_dir / f"{slug}.md"
    if not cache_path.exists():
        return False
    # Check for .stale marker (set by adaptive invalidation)
    stale_path = cache_dir / f"{slug}.stale"
    if stale_path.exists():
        return False
    age = time.time() - cache_path.stat().st_mtime
    return age < max_age


def get_cache_path(slug: str, cache_dir: Path | None = None) -> Path:
    """Return cache file path for an agent."""
    cache_dir = cache_dir or _default_cache_dir()
    return cache_dir / f"{slug}.md"


def read_cache(slug: str, cache_dir: Path | None = None,
               max_age: int = DEFAULT_CACHE_MAX_AGE) -> str | None:
    """Read cached briefing if fresh, else None."""
    cache_dir = cache_dir or _default_cache_dir()
    if not is_cache_fresh(slug, cache_dir, max_age):
        return None
    try:
        return get_cache_path(slug, cache_dir).read_text()
    except OSError:
        return None


def _default_cache_dir() -> Path:
    return Path.home() / ".claude" / "memory" / "context_cache"


# --- Cache Invalidation (adaptive mode) ---

def invalidate_cache(slugs: list[str], cache_dir: Path | None = None) -> list[str]:
    """Mark agent caches as stale by creating .stale marker files.

    Returns list of slugs that were invalidated.
    """
    cache_dir = cache_dir or _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    invalidated = []
    for slug in slugs:
        cache_path = cache_dir / f"{slug}.md"
        if cache_path.exists():
            stale_path = cache_dir / f"{slug}.stale"
            stale_path.write_text(str(time.time()))
            invalidated.append(slug)
            log.info("Invalidated cache for %s", slug)
    return invalidated


def clear_stale_marker(slug: str, cache_dir: Path | None = None) -> None:
    """Remove .stale marker after successful regeneration."""
    cache_dir = cache_dir or _default_cache_dir()
    stale_path = cache_dir / f"{slug}.stale"
    if stale_path.exists():
        stale_path.unlink()


def get_agent_status(slug: str, config: MemoryConfig | None = None) -> dict:
    """Get cache/briefing status for an agent.

    Returns dict with: slug, has_cache, is_fresh, is_stale, enabled, model,
    template_type, size_bytes, generated_at, age_seconds.
    """
    config = config or load_config()
    cache_dir = config.cache_dir
    cache_path = get_cache_path(slug, cache_dir)
    agent_briefing = config.get_agent_briefing(slug)
    stale_path = cache_dir / f"{slug}.stale"

    has_cache = cache_path.exists()
    generated_at = cache_path.stat().st_mtime if has_cache else None
    age = time.time() - generated_at if generated_at is not None else None
    size = cache_path.stat().st_size if has_cache else 0

    return {
        "slug": slug,
        "has_cache": has_cache,
        "is_stale": stale_path.exists(),
        "is_fresh": is_cache_fresh(slug, cache_dir,
                                    agent_briefing.get("cache_max_age", DEFAULT_CACHE_MAX_AGE)),
        "enabled": config.get_agent_enabled(slug),
        "model": agent_briefing.get("model", DEFAULT_MODEL),
        "template_type": config.get_agent_template(slug) or config.get_agent_type(slug),
        "size_bytes": size,
        "generated_at": generated_at,
        "age_seconds": round(age) if age is not None else None,
    }


def _save_generation_log(slug: str, entry: dict, cache_dir: Path,
                         max_entries: int = MAX_LOG_ENTRIES) -> None:
    """Append a log entry to <cache_dir>/<slug>.log.json, rotating old entries."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_path = cache_dir / f"{slug}.log.json"
    entries: list[dict] = []
    if log_path.exists():
        try:
            entries = _json.loads(log_path.read_text())
        except (_json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    entries = entries[-max_entries:]
    log_path.write_text(_json.dumps(entries, indent=2))


def get_generation_logs(slug: str, config: MemoryConfig | None = None) -> list[dict]:
    """Read generation log entries for an agent."""
    config = config or load_config()
    log_path = config.cache_dir / f"{slug}.log.json"
    if not log_path.exists():
        return []
    try:
        return _json.loads(log_path.read_text())
    except (_json.JSONDecodeError, OSError):
        return []


def scope_to_agents(scope: str, config: MemoryConfig) -> list[str]:
    """Map a write scope to affected agent slugs.

    A write to scope X invalidates:
    - Agent X itself (if it's a known agent)
    - Parent agent (if X is a hierarchy child)
    - Orchestrator agents (they see everything)
    """
    affected: set[str] = set()
    all_known = set(config.all_agents())

    # The scope itself
    if scope in all_known:
        affected.add(scope)

    # Parent: if scope is a hierarchy child, parent is affected
    for parent, children in config.hierarchy.items():
        if scope in children:
            affected.add(parent)

    # Orchestrator agents always affected
    for slug in config.agent_types.get("orchestrator", []):
        affected.add(slug)

    return sorted(affected)


# --- Orchestrator Context Assembly ---

def _assemble_orchestrator_context(store: MemoryStore, budget: int) -> str:
    """Build comprehensive raw context for orchestrator — ALL scopes, ALL entities."""
    sections: list[str] = []

    # People (all)
    people = store.list_entities(entity_type="person")
    if people:
        lines = []
        for p in people:
            slots = store.get_slots(p["id"])
            if not slots:
                continue
            s = ", ".join(f"{k}: {v}" for k, v in slots.items())
            line = f"- **{p['name']}** ({s})"
            obs = store.get_observations(p["id"])
            if obs:
                for o in obs[:5]:
                    line += f"\n  - {o['text']}"
            lines.append(line)
        if lines:
            sections.append("## People\n" + "\n".join(lines))

    # Clients & Agencies
    for etype in ("agency", "client"):
        entities = store.list_entities(entity_type=etype)
        if entities:
            lines = []
            for e in entities:
                slots = store.get_slots(e["id"])
                s = ", ".join(f"{k}: {v}" for k, v in slots.items()) if slots else "no data"
                lines.append(f"- **{e['name']}** ({s})")
            sections.append(f"## {etype.title()}s\n" + "\n".join(lines))

    # Projects
    projects = store.list_entities(entity_type="project")
    if projects:
        lines = []
        for p in projects:
            slots = store.get_slots(p["id"])
            s = ", ".join(f"{k}: {v}" for k, v in slots.items()) if slots else "no data"
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
    rel_lines = []
    seen_rels: set[int] = set()
    for e in all_entities:
        rels = store.get_relations(e["id"])
        for r in rels:
            if r["id"] not in seen_rels:
                seen_rels.add(r["id"])
                rel_lines.append(
                    f"- {e['name']} \u2014[{r['type']}]\u2192 {r['to_name']}"
                    + (f" ({r['context']})" if r.get("context") else ""))
    if rel_lines:
        sections.append("## Relations\n" + "\n".join(rel_lines[:50]))

    # Recent Logs (last 20 across all entities)
    log_lines = []
    for e in all_entities:
        logs = store.get_logs(e["id"], limit=3)
        for entry in logs:
            log_lines.append(
                (entry["date"], f"- [{entry['date']}] {e['name']}: {entry['text']}"))
    log_lines.sort(key=lambda x: x[0], reverse=True)
    if log_lines:
        sections.append("## Recent Log\n" + "\n".join(line for _, line in log_lines[:20]))

    result = "\n\n".join(sections)
    return result[:budget] if len(result) > budget else result


# --- Topic Context Assembly ---

def _assemble_topic_context(store: MemoryStore, slug: str, chain: list[str],
                            budget: int) -> str:
    """Build rich context for topic agents — topic entity + related + scoped data."""
    sections: list[str] = []
    chain_set = set(chain)

    # Topic entity itself
    topic_eid = store.find_entity(slug, "topic")
    if topic_eid:
        slots = store.get_slots(topic_eid)
        obs = store.get_observations(topic_eid)
        lines = []
        if slots:
            lines.append("**Slots:** " + ", ".join(f"{k}: {v}" for k, v in slots.items()))
        for o in obs:
            lines.append(f"- {o['text']}")
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
        s = ", ".join(f"{k}: {v}" for k, v in slots.items()) if slots else ""
        line = f"- **{e['name']}** ({e['type']}" + (f", {s}" if s else "") + ")"
        obs = store.get_observations(e["id"])
        visible = [o for o in obs if o.get("scope") in chain_set]
        for o in visible[:8]:
            line += f"\n  - {o['text']}"
        entity_lines.append(line)

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
            s = ", ".join(f"{k}: {v}" for k, v in slots.items()) if slots else ""
            line = f"- **{e['name']}** ({e['type']}" + (f", {s}" if s else "") + ")"
            obs = store.get_observations(e["id"])
            visible = [o for o in obs if o.get("scope") in chain_set]
            for o in visible[:5]:
                line += f"\n  - {o['text']}"
            entity_lines.append(line)

    if entity_lines:
        sections.append("## Related Entities\n" + "\n".join(entity_lines))

    # Relations involving topic-scoped entities
    rel_lines = []
    seen_rels: set[int] = set()
    for e in scoped_entities:
        rels = store.get_relations(e["id"])
        for r in rels:
            if r["id"] not in seen_rels:
                seen_rels.add(r["id"])
                rel_lines.append(
                    f"- {e['name']} \u2014[{r['type']}]\u2192 {r['to_name']}"
                    + (f" ({r['context']})" if r.get("context") else ""))
    if rel_lines:
        sections.append("## Relations\n" + "\n".join(rel_lines))

    # Logs
    all_scoped = store.list_entities_in_scopes(list(chain_set))
    log_lines = []
    for e in all_scoped:
        logs = store.get_logs(e["id"], limit=5)
        for entry in logs:
            log_lines.append(
                (entry["date"], f"- [{entry['date']}] {e['name']}: {entry['text']}"))
    log_lines.sort(key=lambda x: x[0], reverse=True)
    if log_lines:
        sections.append("## Recent Log\n" + "\n".join(line for _, line in log_lines[:15]))

    result = "\n\n".join(sections)
    return result[:budget] if len(result) > budget else result


# --- Context File Loading ---

def _load_context_files(paths: list[Path], budget: int) -> str:
    """Read files and concatenate, truncating to budget.

    Missing or unreadable files are skipped with a log warning.
    """
    sections: list[str] = []
    used = 0
    for path in paths:
        try:
            content = path.read_text()
        except OSError:
            log.warning("Cannot read context file: %s", path)
            continue
        remaining = budget - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n... (truncated)"
        sections.append(f"### {path.name}\n{content}")
        used += len(content)
    return "\n\n".join(sections)


# --- Generation ---

def generate_briefing(
    slug: str,
    config: MemoryConfig | None = None,
    force: bool = False,
    llm_caller: LLMCaller | None = None,
    store: MemoryStore | None = None,
) -> Path | None:
    """Generate AI briefing for one agent. Returns cache path or None.

    Args:
        slug: Agent identifier.
        config: Memory configuration. Loaded from default paths if None.
        force: Regenerate even if cache is fresh.
        llm_caller: Custom LLM invocation function. Signature: (prompt, model, timeout) -> str|None.
                    Defaults to calling `claude -p` CLI.
        store: Optional shared MemoryStore. If provided, caller is responsible for closing it.
    """
    config = config or load_config()

    if not config.get_agent_enabled(slug):
        log.info("Agent %s is disabled, skipping", slug)
        return None

    cache_dir = config.cache_dir
    # Per-agent briefing settings (merged with global defaults)
    agent_briefing = config.get_agent_briefing(slug)
    cache_max_age = agent_briefing.get("cache_max_age", DEFAULT_CACHE_MAX_AGE)
    raw_budget = agent_briefing.get("raw_budget", DEFAULT_RAW_BUDGET)
    output_budget = agent_briefing.get("output_budget", DEFAULT_OUTPUT_BUDGET)
    model = agent_briefing.get("model", DEFAULT_MODEL)
    timeout = agent_briefing.get("timeout", DEFAULT_TIMEOUT)
    caller = llm_caller or default_llm_caller

    cache_path = get_cache_path(slug, cache_dir)

    if not force and is_cache_fresh(slug, cache_dir, cache_max_age):
        log.info("Cache fresh for %s, skipping", slug)
        return cache_path

    agent = config.get_agent(slug)
    if agent.tier == 0:
        log.info("Tier 0 agent %s, skipping", slug)
        return None

    own_store = store is None
    if own_store:
        store = MemoryStore(config.db_path)
    try:
        agent_type = config.get_agent_type(slug)
        # Check if it's a topic entity in the DB
        if store.find_entity(slug, "topic") is not None:
            agent_type = "topic"

        if agent_type == "orchestrator":
            raw = _assemble_orchestrator_context(store, budget=raw_budget)
        elif agent_type == "topic":
            raw = _assemble_topic_context(store, slug, agent.chain, budget=raw_budget)
        else:
            raw = assemble_context(
                store, chain=agent.chain, tier=agent.tier, budget=raw_budget,
                vault_projects_dir=(config.vault_dir / "projects"
                                    if config.vault_dir else None),
                task_header=config.vault_task_header,
            )
    finally:
        if own_store:
            store.close()

    # Append per-agent extra context from config
    extra = config.get_agent_extra_context(slug)
    if extra:
        raw = (raw or "") + f"\n\n## Additional Context\n{extra}"

    # Append content from context_files
    ctx_files = config.get_agent_context_files(slug)
    if ctx_files:
        ctx_budget = config.get_agent_context_budget(slug)
        file_content = _load_context_files(ctx_files, ctx_budget)
        if file_content:
            raw = (raw or "") + f"\n\n## Project Context\n{file_content}"

    if not raw or len(raw.strip()) < 50:
        log.info("No meaningful raw context for %s (%d chars)", slug, len(raw or ""))
        return None

    # Check for per-agent template override
    custom_template = config.get_agent_template(slug)
    if custom_template and custom_template in BUILTIN_TEMPLATES:
        # Type name override — use that builtin template instead of auto-detected
        agent_type = custom_template
        prompt = build_prompt(slug, agent_type, raw, output_budget, config.templates_dir)
    elif custom_template:
        # Inline custom template — format directly
        prompt = custom_template.format(slug=slug, raw_context=raw, budget=output_budget)
    else:
        prompt = build_prompt(slug, agent_type, raw, output_budget, config.templates_dir)
    log.info("Generating briefing for %s (%s, %d chars raw)", slug, agent_type, len(raw))

    start_time = time.time()
    raw_result = caller(prompt, model, timeout)
    duration_ms = int((time.time() - start_time) * 1000)

    # Normalize — support both str|None and LLMResult
    if isinstance(raw_result, LLMResult):
        result = raw_result.text
        input_tokens = raw_result.input_tokens
        output_tokens = raw_result.output_tokens
    else:
        result = raw_result
        input_tokens = None
        output_tokens = None

    log_entry = {
        "slug": slug,
        "timestamp": time.time(),
        "model": model,
        "agent_type": agent_type,
        "duration_ms": duration_ms,
        "input_chars": len(prompt),
        "output_chars": len(result) if result else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    if not result:
        log.warning("LLM returned empty for %s", slug)
        log_entry["status"] = "error:empty_response"
        _save_generation_log(slug, log_entry, cache_dir)
        return None

    log_entry["status"] = "ok"

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(result)
    clear_stale_marker(slug, cache_dir)
    _save_generation_log(slug, log_entry, cache_dir)
    log.info("Cached briefing for %s (%d chars)", slug, len(result))
    return cache_path


def generate_all(
    agent_slugs: list[str] | None = None,
    config: MemoryConfig | None = None,
    force: bool = False,
    llm_caller: LLMCaller | None = None,
) -> dict[str, str]:
    """Generate briefings for multiple agents. Returns {slug: status}.

    Args:
        agent_slugs: List of agent slugs to process. If None, uses config.all_agents().
        config: Memory configuration.
        force: Regenerate even if cache is fresh.
        llm_caller: Custom LLM invocation function.
    """
    config = config or load_config()
    slugs = agent_slugs or config.all_agents()
    results: dict[str, str] = {}

    with MemoryStore(config.db_path) as store:
        for slug in sorted(slugs):
            if not config.get_agent_enabled(slug):
                results[slug] = "skip:disabled"
                continue
            agent = config.get_agent(slug)
            if agent.tier == 0:
                results[slug] = "skip:tier0"
                continue

            try:
                path_result = generate_briefing(slug, config=config, force=force,
                                                llm_caller=llm_caller, store=store)
                if path_result:
                    results[slug] = "ok"
                else:
                    results[slug] = "skip:no_context"
            except Exception as e:
                log.error("Failed to generate for %s: %s", slug, e)
                results[slug] = f"error:{e}"

    return results
