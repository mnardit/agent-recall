# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
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
