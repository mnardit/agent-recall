# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.2.1] - 2026-02-21

### Added
- `store.transaction()` context manager for atomic multi-step operations
- `store.count_scope()` — count active slots, observations, relations in a scope
- `ScopedView.get_entity()` now returns `observations` (filtered by scope chain)
- `ScopedView.add_log()` returns `bool` indicating success
- `MCPBridge` implements context manager protocol (`with MCPBridge(...) as b:`)
- "Key Concepts" table in README (entity, slot, observation, scope, tier, briefing)

### Fixed
- **Quickstart example crash** — `KeyError: 'observations'` on line 25 (flagship example)
- **Version mismatch** — `__init__.py` said 0.1.0 while `pyproject.toml` said 0.2.0
- `merge_entities()` now truly atomic via `store.transaction()` (was pseudo-atomic)
- `delete_observations()` returns accurate count (uses `rowcount` instead of always +1)
- `delete_observation_by_text()` returns number of rows affected
- `delete_relations()` validates input dict structure (was raising KeyError on malformed input)
- CLI `get` shows error message on missing slot (was silent exit code 1)
- CLI `rename-scope --dry-run` uses public `store.count_scope()` (was accessing private `_conn`)
- README test count updated to match actual count
- Module docstrings clarify `context.py` (raw assembly) vs `context_gen.py` (LLM briefings)

## [0.2.0] - 2026-02-20

### Added
- `strict_scopes` parameter on `MCPBridge` — validates that all scope chains contain only known scopes from config, rejects unknown scopes at write time
- `MemoryConfig.known_scopes()` method — returns set of all scopes defined in hierarchy + tiers + agents
- `store.rename_scope(old, new)` — migrates all slots, observations, and log entries from one scope to another. Also available as CLI command `agent-recall rename-scope <old> <new>`
- Improved context assembly for deep scope chains — people split into primary (leaf-scoped) and secondary (inherited) sections
- Parent context section in briefings for agents in deep scope chains (3+ levels)
- Leaf-scoped log filtering — agents in deep scope chains see only their own scope's activity log, not the entire parent chain
- Warning on unknown slug in `get_agent()` fallback — helps catch misconfigured agent names early
- Auto-discovery of project files (CLAUDE.md, README.md, .cursorrules, .windsurfrules) for richer briefings — ON by default, disable with `auto_discover: false`
- Per-agent model override via `agents.<slug>.model` in config
- Per-agent template override via `agents.<slug>.template` (type name or inline text)
- Agent enabled/disabled toggle via `agents.<slug>.enabled`
- Adaptive cache invalidation with `.stale` markers
- Cross-agent triggers — writes to child scope invalidate parent and orchestrator
- `context_files` — load external files into briefing context
- `get_agent_status()` — query cache metadata (freshness, size, timestamps)
- `get_all_statuses()` — batch status for all agents
- `get_generation_logs()` — read generation history per agent
- `LLMResult` dataclass for structured LLM responses with token tracking
- Generation logging with rotation (last 10 entries per agent)
- ISO 8601 timestamps in status responses (`generated_at_iso`)
- Comprehensive docstrings on `MemoryStore`, `MCPBridge`, `assemble_context`
- `py.typed` marker for PEP 561 type checking support
- Configurable briefing backend: `cli` (default, free) or `api` (Anthropic SDK)
- `MemoryStore.rollback()` public method for error recovery
- MCP server instructions — agents proactively save facts without prompting
- Cold-start message when memory is empty (hooks)
- Model aliases: `opus`, `sonnet`, `haiku` resolve to full model IDs
- CHANGELOG.md

### Fixed
- MCP bridge now reports "Entity not found" in `blocked` when `add_observations`, `delete_observations`, `delete_entities`, or `delete_relations` reference a nonexistent entity — previously silently dropped
- `list_entities_in_scopes()` now finds entities with observations-only (no slots) via UNION query — previously invisible
- `assemble_context()` now includes "Project Context" section (observations for leaf-scope entities) at tier >= 1
- `build_prompt()` crash on `{` in slug or raw context (both now escaped before `.format()`)
- Path traversal in vault `_safe_filename()` — uses `Path.name` to strip directory components
- Git auto-commit race condition — commit now blocks before push starts
- MCP bridge input validation — missing/malformed fields return errors instead of crashing
- `merge_entities()` uses public `store.rollback()` instead of private `_conn.rollback()`
- Unused `MemoryConfig` import removed from MCP server
- CLI model parameter validated against safe character set
- `_load_context_files()` skips non-regular files (directories, devices)
- Fragile dict key access in log assembly (uses `.get()` with defaults)
- `get_agent_briefing()` now merges `min_cache_age` and `adaptive` per-agent
- `get_agent_status()` caches `stat()` call (was calling twice)
- `load_config()` raises `ValueError` on malformed YAML (was unhandled `ParserError`)
- `get_agent_status()` raises `ValueError` on empty slug
- Hardcoded `/tmp/` paths replaced with `tempfile.gettempdir()` (symlink attack prevention)

### Changed
- `mcp` is now an optional dependency: `pip install 'agent-recall[mcp]'`
- `api` optional dependency added: `pip install 'agent-recall[api]'`
- MCP server singleton documents single-threaded assumption

### Removed
- Dead modules: `episodes.py` (episodic memory) and `schema.py` (unused schema definitions)

## [0.1.0] - 2026-02-15

### Added
- Initial release
- SQLite memory store with bitemporal slots, observations, relations
- Scope hierarchy with inheritance and enforcement
- MCP server (FastMCP) for Claude Code integration
- AI briefing generation with configurable LLM and prompt templates
- 6 built-in prompt templates: client, agency, personal, topic, system, orchestrator
- SessionStart hook with cache-first strategy
- PostToolUse hook for vault regeneration
- YAML configuration system
- CLI: `agent-recall init / set / get / search / generate / status`
- 249 tests
