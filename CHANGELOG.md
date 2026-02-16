# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
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
- CHANGELOG.md

### Fixed
- `get_agent_briefing()` now merges `min_cache_age` and `adaptive` per-agent
- `get_agent_status()` caches `stat()` call (was calling twice)
- `load_config()` raises `ValueError` on malformed YAML (was unhandled `ParserError`)
- `get_agent_status()` raises `ValueError` on empty slug

### Changed
- Dependency pinning: `mcp>=1.0,<2.0` (was `mcp>=1.0`)

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
- CLI: `claude-memory init / set / get / search / generate / status`
- 249 tests
