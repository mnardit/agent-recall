"""Main briefing generation entry points."""
import logging
import time
from pathlib import Path
from typing import Callable

from agent_recall.config import MemoryConfig, load_config
from agent_recall.context import assemble_context
from agent_recall.store import MemoryStore

from agent_recall.context_gen.cache import (
    DEFAULT_CACHE_MAX_AGE,
    clear_stale_marker,
    get_cache_path,
    is_cache_fresh,
    _save_generation_log,
)
from agent_recall.context_gen.templates import (
    BUILTIN_TEMPLATES,
    DEFAULT_OUTPUT_BUDGET,
    build_prompt,
)
from agent_recall.context_gen.llm import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    LLMCaller,
    LLMResult,
    _get_default_caller,
)
from agent_recall.context_gen.files import (
    _discover_project_files,
    _extract_claude_md_sections,
    _load_context_files,
)
from agent_recall.context_gen.assembly import (
    _assemble_orchestrator_context,
    _assemble_topic_context,
)

log = logging.getLogger(__name__)

DEFAULT_RAW_BUDGET = 50000     # generous budget for raw input


def generate_briefing(
    slug: str,
    config: MemoryConfig | None = None,
    force: bool = False,
    llm_caller: LLMCaller | None = None,
    store: MemoryStore | None = None,
    project_dir: Path | None = None,
) -> Path | None:
    """Generate AI briefing for one agent. Returns cache path or None.

    Args:
        slug: Agent identifier.
        config: Memory configuration. Loaded from default paths if None.
        force: Regenerate even if cache is fresh.
        llm_caller: Custom LLM invocation function. Signature: (prompt, model, timeout) -> str|None.
                    Defaults to calling `claude -p` CLI.
        store: Optional shared MemoryStore. If provided, caller is responsible for closing it.
        project_dir: Directory to scan for project files (CLAUDE.md, README.md, etc.).
                     Defaults to CWD if not specified.
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
    caller = llm_caller or _get_default_caller(config)

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
        # Check if it's a topic entity in the DB (but not for orchestrators)
        orchestrators = set(config.agent_types.get("orchestrator", []))
        if slug not in orchestrators and store.find_entity(slug, "topic") is not None:
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

    # Append content from context_files (explicit config + auto-discovered)
    ctx_files = config.get_agent_context_files(slug)

    # Auto-discover project files from project_dir or CWD
    if config.briefing.get("auto_discover", True):
        discovered = _discover_project_files(project_dir)
        # Merge: explicit config files first, then discovered (no duplicates)
        seen = {f.resolve() for f in ctx_files}
        for f in discovered:
            if f.resolve() not in seen:
                ctx_files.append(f)
                seen.add(f.resolve())

    if ctx_files:
        ctx_budget = config.get_agent_context_budget(slug)
        if project_dir:
            allowed_bases = [project_dir]
        else:
            allowed_bases = [config.db_path.parent]
            if Path.home().exists():
                allowed_bases.append(Path.home())
        file_content = _load_context_files(
            ctx_files, ctx_budget,
            allowed_bases=allowed_bases)
        if file_content:
            raw = (raw or "") + f"\n\n## Project Files\n{file_content}"

        # Extract key CLAUDE.md sections and add as top-level for prominence
        extracted = _extract_claude_md_sections(ctx_files)
        if extracted.get("constraints"):
            raw = (raw or "") + (
                f"\n\n## Agent Constraints (from CLAUDE.md)\n"
                f"{extracted['constraints']}"
            )
        if extracted.get("people_roles"):
            raw = (raw or "") + (
                f"\n\n## Role Descriptions (from CLAUDE.md)\n"
                f"{extracted['people_roles']}"
            )

    if not raw or len(raw.strip()) < 50:
        log.info("No meaningful raw context for %s (%d chars)", slug, len(raw or ""))
        return None

    # Check for per-agent template override
    custom_template = config.get_agent_template(slug)
    if custom_template and custom_template in BUILTIN_TEMPLATES:
        # Type name override -- use that builtin template instead of auto-detected
        agent_type = custom_template
        prompt = build_prompt(slug, agent_type, raw, output_budget, config.templates_dir)
    elif custom_template:
        # Inline custom template -- use manual replacement to prevent format string injection.
        # str.format() allows attribute access ({__class__.__init__...}) which is unsafe
        # when template comes from config that could be written by agents.
        prompt = (custom_template
                  .replace("{slug}", slug)
                  .replace("{raw_context}", raw)
                  .replace("{budget}", str(output_budget)))
    else:
        prompt = build_prompt(slug, agent_type, raw, output_budget, config.templates_dir)
    log.info("Generating briefing for %s (%s, %d chars raw)", slug, agent_type, len(raw))

    start_time = time.time()
    raw_result = caller(prompt, model, timeout)
    duration_ms = int((time.time() - start_time) * 1000)

    # Normalize -- support both str|None and LLMResult
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
    cache_path.write_text(result, encoding="utf-8")
    clear_stale_marker(slug, cache_dir)
    _save_generation_log(slug, log_entry, cache_dir)
    log.info("Cached briefing for %s (%d chars)", slug, len(result))
    return cache_path


def generate_all(
    agent_slugs: list[str] | None = None,
    config: MemoryConfig | None = None,
    force: bool = False,
    llm_caller: LLMCaller | None = None,
    project_dir_map: dict[str, Path] | None = None,
    slug_filter: Callable[[str], bool] | None = None,
) -> dict[str, str]:
    """Generate briefings for multiple agents. Returns {slug: status}.

    Args:
        agent_slugs: List of agent slugs to process. If None, uses config.all_agents().
        config: Memory configuration.
        force: Regenerate even if cache is fresh.
        llm_caller: Custom LLM invocation function.
        project_dir_map: Optional mapping of slug to project directory for file
            auto-discovery. Without this, auto-discovery scans CWD (usually wrong
            in batch mode).
        slug_filter: Optional predicate. Called with each slug; return False to skip.
            Applied after agent_slugs filtering but before generation.
    """
    config = config or load_config()
    slugs = agent_slugs or config.all_agents()
    project_dir_map = project_dir_map or {}
    results: dict[str, str] = {}

    with MemoryStore(config.db_path) as store:
        for slug in sorted(slugs):
            if slug_filter is not None and not slug_filter(slug):
                results[slug] = "skip:filtered"
                continue
            if not config.get_agent_enabled(slug):
                results[slug] = "skip:disabled"
                continue
            if config.get_agent(slug).tier == 0:
                results[slug] = "skip:tier0"
                continue

            try:
                path = generate_briefing(
                    slug, config=config, force=force,
                    llm_caller=llm_caller, store=store,
                    project_dir=project_dir_map.get(slug),
                )
                results[slug] = "ok" if path else "skip:no_context"
            except Exception as e:
                log.error("Failed to generate for %s: %s", slug, e)
                results[slug] = f"error:{e}"

    return results
