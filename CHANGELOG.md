# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **Full-text search (FTS5)** with Porter stemmer. Falls back to LIKE if FTS5 is unavailable. Use `rebuild_fts()` to index existing databases
- **Schema migrations** — versioned, atomic, run automatically on DB open
- **Claude Code Plugin** — install with `/plugins add mnardit/agent-recall`
- **CLI commands:** `observe`, `delete`, `templates`
- **Explicit `scope_chain`** in per-agent config (overrides inferred chain)
- **`database` config alias** for `db_path`
- Architecture docs (`docs/architecture.md`), Quick Start and auto-memory comparison in README

### Fixed
- **Write atomicity** — `set_slot` and similar writes are now wrapped in `BEGIN IMMEDIATE` outside explicit transactions
- **Transaction flag ordering** — `_in_transaction` cleared after commit/rollback, not before
- **`get_agent()` ignored explicit `scope_chain`** from config
- **`merge_entities()` partial merges** — nested `BEGIN IMMEDIATE` broke atomicity
- **Orchestrator template hijack** — `create_entities(name=slug, entityType="topic")` no longer downgrades orchestrator briefings
- **`get_logs()` returned oldest N** instead of newest N
- **Cross-scope relation injection** — source entity must now be writable
- **Cache invalidation** accepted fabricated scope values; now validates against agent's chain
- **`list_entities_in_scopes([])`** generated invalid SQL
- **`read_graph()` duplicate relations**
- **Non-string observations** now validated consistently
- **Silent truncation** — methods that cap at 100 items now report it
- Softer error wording ("Write blocked" instead of "SCOPE VIOLATION")
- Hook rate-limit race condition, vault filename collisions, CLI error messages, API caller crash on empty response, config list validation

### Changed
- **`context_gen` split into package** — 7 submodules, all imports backward compatible
- **`vault_gen` and `dedup` moved to `contrib/`** — import from `agent_recall.contrib.*`
- Deprecated `default_llm_caller` and top-level `dedup`/`vault_gen` shims removed
- DB file permissions set to `0o600`, directory to `0o700`
- GitHub Actions pinned to commit SHAs
- MCP bridge registered with `atexit` for cleanup

### Security
- **Prompt injection** — observations XML-escaped (`&`, `<`, `>`) before wrapping in `<observation>` tags
- **Path traversal** — slugs sanitized in cache paths; `load_template()` validates `is_relative_to()`; `context_files` always checks `allowed_bases`
- **MCP input limits** — all 6 write methods capped at 100 items per call; relation type max 200 chars; `read_graph` relations capped at 5000

## [0.3.0] - 2026-02-27

### Added
- **`scope_reads` parameter on `MCPBridge`** — generic boolean to control whether read operations (`search_nodes`, `open_nodes`, `read_graph`) filter results by scope chain. Replaces hardcoded agent-type checks with a clean, configurable option. Default: `True` (filtered)
- **Scope filtering on `open_nodes()` and `read_graph()`** — these MCP tools now respect scope isolation, matching `search_nodes()` behavior. Previously they returned all entities regardless of scope, making scope enforcement incomplete
- **Observation cap (20 per entity)** on all read operations — prevents context bloat when entities accumulate many observations
- **Configurable `default_agent_type`** in briefing config — `briefing.default_agent_type` setting, defaults to `"personal"`
- **Per-agent type override** via `agents.<slug>.type` in config — takes precedence over inference from hierarchy/tiers

### Fixed
- **Scope isolation on reads was incomplete** — `open_nodes()` and `read_graph()` bypassed scope filtering entirely, allowing any agent to read the full knowledge graph. Now all three read methods use shared scope helpers (`_read_scope_set`, `_entity_visible`, `_filter_observations`)
- **`get_agent()` tiers vs hierarchy precedence** — agents listed in both `tiers` and `hierarchy.children` silently lost their parent scope from the chain. Now checks hierarchy inside the tiers branch to preserve the full chain
- **`MCPBridge` resource leak** — if `strict_scopes` validation raised after `MemoryStore` was created, the database connection leaked. Constructor now wraps post-init logic in try/except to close on failure
- **`hooks.py` `lock_fd` NameError** — if `lock_file.open()` raised `OSError`, the `finally` block crashed on undefined `lock_fd`
- **Explicit `encoding="utf-8"`** on all `read_text()` / `write_text()` calls across config, context, context_gen, and vault_gen modules — prevents encoding errors on Windows with non-ASCII content
- **Invalid tier key error** — `_parse_config()` now raises a clear `ValueError` with file path and key when a non-integer tier key is found

### Changed
- **Briefing templates use neutral terminology** — "client project" → "project", "agency/organization with multiple sub-clients" → "group of related projects and teams", "Clients & Projects" → "Projects". Templates are now domain-agnostic
- `vault_gen._git_auto_commit()` accepts configurable `git_paths` parameter (was hardcoded)

### Removed
- `ScopedView.add_log()` — dead method, unused since log writes go through `MemoryStore.add_log()` directly

## [0.2.4] - 2026-02-26

### Changed
- **`set` command: entity type is now `--type` option** (was broken positional argument). Usage: `agent-recall set Alice role Engineer --type person`
- **Model alias `sonnet`** now maps to `claude-sonnet-4-6` (was legacy `claude-sonnet-4-5`)
- Claude Code hooks example in README updated to current format (`type`, `matcher`, nested `hooks` array)
- Python 3.13 added to CI matrix and classifiers

### Fixed
- `set` command 3-argument form actually works now (Click couldn't parse optional positional `ENTITY_TYPE`)
- "Zero dependencies" claim corrected to "Minimal dependencies" (we have two: pyyaml, click)

## [0.2.3] - 2026-02-26

### Added
- `search --json` flag for machine-readable output
- `entity --scope` option for scoped slot resolution (repeatable)
- `entity` command now shows observations alongside slots
- `log --author` option (default: "agent")
- `list` and `logs` commands documented in README
- Troubleshooting section in README (empty briefings, Windows python3, MCP connection)
- Expanded Python API examples in README (search, history, transactions)

### Improved
- `set` command — `ENTITY_TYPE` is now optional for existing entities (auto-lookup)
- Editor messaging reframed as "MCP-native" (works everywhere, battle-tested with Claude Code)
- `rename-scope` shows clean error messages instead of Python tracebacks

### Fixed
- **Security: format string injection** in custom briefing templates — replaced `str.format()` with safe manual substitution
- **Security: path traversal** in vault task loading — scope names with `..` are now rejected
- **Security: f-string SQL** in `find_orphaned_scopes()` — replaced with explicit parameterized queries
- Comparison table removed — previous version had inaccurate claims about competitors
- Hierarchy config format in README now matches actual YAML format
- OpenAI and Ollama examples now use the `model` parameter passed by agent-recall
- `examples/memory.yaml` now includes `backend`, `agents`, `adaptive`, `auto_discover` sections

## [0.2.2] - 2026-02-22

### Added
- `store.find_entities_by_slot(key, value, entity_type, scope)` — single-query entity lookup by slot value
- `store.find_orphaned_scopes()` — find scopes with no matching hierarchy or agent config
- `store.find_duplicate_slots()` — detect duplicate active slots (same entity + key + scope)
- `store.find_thin_entities()` — find entities with no slots and no observations
- `store.check_integrity()` — run all integrity checks, returns structured report
- `generate_all()` now accepts `project_dir_map` and `slug_filter` for production batch use
- `AGENT_RECALL_DB_PATH` / `AGENT_RECALL_CACHE_DIR` environment variables (precedence: yaml > env > default)
- `create_entities` MCP tool response now includes `updated` count (entities that already existed)

### Fixed
- Removed unused `commit()` / `rollback()` public methods from `MemoryStore` (use `transaction()` instead)
- Removed unused `AGENT_TYPES` constant from `context_gen.py`
- Removed unused `DEFAULT_CONFIG_PATHS` constant from `config.py`
- Cleaned up 3 unused test imports (`AgentConfig`, `DISCOVERABLE_FILES`, `get_not_same_pairs`)
- Ollama example now returns `LLMResult` (was returning plain `str`)
- Test data uses neutral names instead of author-identifiable data

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
