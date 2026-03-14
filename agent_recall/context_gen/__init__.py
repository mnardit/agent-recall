"""AI Briefing Generation -- LLM summarizes raw context into agent briefings.

Uses raw data from context.py, applies prompt templates, sends to LLM,
and caches the result. Includes cache management, adaptive invalidation,
template loading, and generation logging.

For raw data assembly (no LLM) see context.py.
"""

# Re-export public names only.
# Internal helpers (_prefixed) are accessed via their submodules directly.

from agent_recall.context_gen.cache import (
    DEFAULT_CACHE_MAX_AGE,
    MAX_LOG_ENTRIES,
    is_cache_fresh,
    get_cache_path,
    read_cache,
    invalidate_cache,
    clear_stale_marker,
    scope_to_agents,
    get_agent_status,
    get_all_statuses,
    get_generation_logs,
)

from agent_recall.context_gen.templates import (
    DEFAULT_OUTPUT_BUDGET,
    BUILTIN_TEMPLATES,
    get_template,
    load_template,
    list_templates,
    build_prompt,
)

from agent_recall.context_gen.llm import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    LLMCaller,
    LLMResult,
    MODEL_ALIASES,
)

from agent_recall.context_gen.files import (
    DISCOVERABLE_FILES,
)

from agent_recall.context_gen.assembly import (
    MAX_OBSERVATION_LENGTH,
)

from agent_recall.context_gen.generator import (
    DEFAULT_RAW_BUDGET,
    generate_briefing,
    generate_all,
)

__all__ = [
    # Cache
    "DEFAULT_CACHE_MAX_AGE",
    "MAX_LOG_ENTRIES",
    "is_cache_fresh",
    "get_cache_path",
    "read_cache",
    "invalidate_cache",
    "clear_stale_marker",
    "scope_to_agents",
    "get_agent_status",
    "get_all_statuses",
    "get_generation_logs",
    # Templates
    "DEFAULT_OUTPUT_BUDGET",
    "BUILTIN_TEMPLATES",
    "get_template",
    "load_template",
    "list_templates",
    "build_prompt",
    # LLM
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "LLMCaller",
    "LLMResult",
    "MODEL_ALIASES",
    # Files
    "DISCOVERABLE_FILES",
    # Assembly
    "MAX_OBSERVATION_LENGTH",
    # Generator
    "DEFAULT_RAW_BUDGET",
    "generate_briefing",
    "generate_all",
]
