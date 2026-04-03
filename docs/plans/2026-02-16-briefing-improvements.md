# Briefing Improvements Plan

4 features for claude-memory package + dashboard coordination.

**Constraint:** Library stays generic/public. Dashboard integration is bigboss-side only.

---

## Task 1: Per-agent model override

**Goal:** Allow per-agent briefing settings (model, timeout, output_budget) in YAML config.

**Config format:**
```yaml
briefing:
  model: haiku           # default
  timeout: 120

agents:
  bigboss:
    model: opus
    timeout: 300
    output_budget: 12000
  knestel:
    model: sonnet
```

**Files (claude-memory):**
- `claude_memory/config.py`: add `agents: dict[str, dict[str, Any]]` to MemoryConfig, parse from YAML
- `claude_memory/context_gen.py`: in `generate_briefing`, merge agent-specific overrides with defaults
- `tests/test_config.py`: test agents parsing, merge behavior
- `tests/test_context_gen.py`: test per-agent model reaches LLM caller

**Steps:**
1. Add `agents` field to `MemoryConfig` dataclass
2. Parse `agents` section in `_parse_config`
3. Add `get_agent_briefing(slug)` method that merges `briefing` defaults + `agents[slug]` overrides
4. Update `generate_briefing` to use `config.get_agent_briefing(slug)` for model/timeout/budgets
5. Move `extra_context` INTO the `agents` section (breaking: old format still supported via migration)
6. Tests: config parsing, merge precedence, generate_briefing uses correct model
7. Verify: `pytest tests/test_config.py tests/test_context_gen.py -v`

---

## Task 2: Adaptive cache + cross-agent triggers

**Goal:** Invalidate stale caches when MCP writes happen. Affected agents regenerate on next SessionStart.

**Config format:**
```yaml
briefing:
  adaptive: true
  min_cache_age: 1800    # 30 min minimum between regenerations
```

**Mechanism:**
1. PostToolUse hook detects MCP write → determines affected scopes
2. Maps scopes → agent slugs (the agent itself + parent agents)
3. Creates `<slug>.stale` marker file in cache_dir
4. SessionStart hook checks for `.stale` — if exists AND min_cache_age passed → regenerate in background

**Files (claude-memory):**
- `claude_memory/config.py`: add `briefing.adaptive` and `briefing.min_cache_age` support
- `claude_memory/context_gen.py`: add `invalidate_cache(slugs, cache_dir)` and `scope_to_agents(scope, config)`
- `claude_memory/hooks.py`: PostToolUse — call invalidate after vault regen. SessionStart — check stale + regen
- `tests/test_context_gen.py`: test invalidation, scope mapping, stale detection

**Steps:**
1. Add `invalidate_cache(slugs, cache_dir)` — creates `.stale` marker files
2. Add `scope_to_agents(scope, config)` — maps scope to affected agent slugs via hierarchy
3. Update `is_cache_fresh` to also check `.stale` marker
4. Update PostToolUse hook: after vault regen, determine scope from stdin data, invalidate affected agents
5. Update SessionStart hook: if stale + min_cache_age passed, regenerate (sync, before returning context)
6. Tests: invalidation creates markers, scope mapping, stale detection, SessionStart regen
7. Verify: `pytest tests/ -v`

---

## Task 3: context_files — file-based extra context

**Goal:** Per-agent list of files to read and append to raw context at generation time.

**Config format:**
```yaml
agents:
  agency-dashboard:
    context_files:
      - ~/projects/personal/agency-dashboard/CLAUDE.md
    context_budget: 3000
```

**Files (claude-memory):**
- `claude_memory/config.py`: parse `context_files` and `context_budget` from agents section
- `claude_memory/context_gen.py`: `_load_context_files(paths, budget)` → read + truncate, append to raw
- `tests/test_context_gen.py`: test file loading, budget truncation, missing files handled

**Steps:**
1. Parse `context_files` (list of path strings) and `context_budget` (int) in agents section
2. Add `_load_context_files(paths, budget)` helper — reads files, truncates to budget
3. In `generate_briefing`: after extra_context append, also append context_files content
4. Handle missing/unreadable files gracefully (log warning, skip)
5. Tests: file loading, budget, missing files
6. Verify: `pytest tests/ -v`

---

## Task 4: Dashboard coordination

**Goal:** Tell dashboard agent to add UI for viewing/editing per-agent briefing settings from memory.yaml.

**NOT in claude-memory package.** This is bigboss → dashboard delegation.

**Steps:**
1. Define the API contract: what the dashboard needs to read/write from memory.yaml
2. Send task to dashboard agent via tmsend with clear spec
3. Update production memory.yaml with per-agent settings for all active agents

---

## Dependency Order

```
Task 1 (per-agent model) — foundation, agents section
  ↓
Task 2 (adaptive cache) — uses config, extends hooks
  ↓
Task 3 (context_files) — uses agents section from Task 1
  ↓
Task 4 (dashboard) — uses final config format
```

## Verification

After each task: `pytest tests/ -q` in claude-memory (expect all pass).
After Task 2: also `pytest tests/ -q` in bigboss.
After all: regenerate briefings for 2-3 agents, verify output.
