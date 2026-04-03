# claude-memory: Public Package Extraction Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract the production memory system from bigboss into `claude-memory` — a public, pip-installable Python package for persistent AI agent memory with scope hierarchy, MCP server, and AI briefing generation.

**Architecture:** Single SQLite DB with scoped slots (bitemporal), hierarchical scope inheritance, MCP protocol bridge with scope enforcement, and an AI context layer that generates structured briefings from raw data. Package reads all project-specific config (agents, tiers, hierarchy, prompt templates) from a YAML file instead of hardcoded Python constants. The LLM invocation is pluggable (CLI, API, custom callable).

**Tech Stack:** Python 3.10+, SQLite (stdlib), FastMCP (`mcp` package), PyYAML, `click` (CLI). Zero heavyweight dependencies.

**Repo:** `~/projects/personal/claude-memory/` → GitHub `mnardit/claude-memory`

---

## Current State (what we're extracting from)

```
bigboss/lib/memory/
├── store.py          # SQLite storage engine (6 tables, bitemporal slots) — GENERIC
├── hierarchy.py      # ScopedView with chain inheritance — GENERIC
├── episodes.py       # Session summaries as markdown files — GENERIC
├── schema.py         # Frame/slot definitions (unused at runtime) — GENERIC
├── config.py         # Agent tiers, scope chains — HARDCODED (BOBDO_CLIENTS etc.)
├── context.py        # Raw context assembly with priority budgets — MOSTLY GENERIC
├── context_gen.py    # AI briefing generation (prompts, LLM, cache) — HEAVILY HARDCODED
├── mcp_bridge.py     # MCP adapter with scope enforcement — NEEDS CONFIG
├── mcp_server.py     # FastMCP stdio server — NEEDS CONFIG
├── vault_gen.py      # Obsidian Markdown generator — OPTIONAL OUTPUT PLUGIN
├── cli.py            # CLI interface — NEEDS REWRITE (click)
└── __init__.py
```

**Tests:** 147 passing across 12 files. ~101 directly test lib/memory/.

**External deps:** Only `mcp` (FastMCP). Everything else is stdlib.

---

## Target Package Structure

```
claude-memory/
├── pyproject.toml                  # Package metadata, entry points
├── README.md                       # Architecture diagram, quickstart, examples
├── LICENSE                         # MIT
├── claude_memory/
│   ├── __init__.py                 # Package version, public API exports
│   ├── store.py                    # MemoryStore (from lib/memory/store.py, as-is)
│   ├── hierarchy.py                # ScopedView (from lib/memory/hierarchy.py, as-is)
│   ├── schema.py                   # FrameSchema, SlotDef, BUILTIN_SCHEMAS
│   ├── episodes.py                 # Episode save/load (as-is)
│   ├── config.py                   # NEW: YAML-based config loader
│   ├── context.py                  # Raw context assembly (configurable vault path + task header)
│   ├── context_gen.py              # AI briefing gen (configurable templates + LLM)
│   ├── mcp_bridge.py               # MCP bridge (scope children from config)
│   ├── mcp_server.py               # FastMCP server (configurable)
│   ├── vault_gen.py                # Obsidian output (optional, configurable)
│   ├── dedup.py                    # Entity deduplication utilities
│   ├── hooks/
│   │   ├── session_start.py        # SessionStart hook (installable)
│   │   └── post_tool_use.py        # PostToolUse vault regen hook (installable)
│   └── cli.py                      # Click-based CLI
├── templates/                      # Default prompt templates
│   ├── client.md
│   ├── agency.md
│   ├── personal.md
│   ├── topic.md
│   ├── system.md
│   └── orchestrator.md
├── tests/
│   ├── conftest.py                 # Shared fixtures (store, config)
│   ├── test_store.py               # Store CRUD, search, bitemporality
│   ├── test_hierarchy.py           # ScopedView
│   ├── test_config.py              # YAML config loading
│   ├── test_schema.py              # Frame definitions
│   ├── test_episodes.py            # Episode save/load
│   ├── test_context.py             # Raw context assembly
│   ├── test_context_gen.py         # AI briefing generation
│   ├── test_mcp_bridge.py          # MCP bridge + scope enforcement
│   ├── test_vault_gen.py           # Vault output
│   └── test_dedup.py               # Deduplication
└── examples/
    ├── memory.yaml                 # Example config (marketing agency)
    ├── quickstart.py               # Minimal usage
    └── multi_agent_setup/          # Full multi-agent example
        ├── memory.yaml
        ├── templates/
        └── README.md
```

---

## Task 1: Repo Scaffolding + pyproject.toml

**Files:**
- Create: `~/projects/personal/claude-memory/pyproject.toml`
- Create: `~/projects/personal/claude-memory/claude_memory/__init__.py`
- Create: `~/projects/personal/claude-memory/LICENSE`
- Create: `~/projects/personal/claude-memory/.gitignore`

**Step 1: Create repo directory and git init**

```bash
mkdir -p ~/projects/personal/claude-memory
cd ~/projects/personal/claude-memory
git init
```

**Step 2: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "claude-memory"
version = "0.1.0"
description = "Persistent memory with scope hierarchy for AI agents in Claude Code"
readme = "README.md"
license = "MIT"
requires-python = ">=3.10"
authors = [{ name = "Max Nardit", email = "max@nardit.com" }]
keywords = ["claude", "ai", "memory", "mcp", "agents"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Topic :: Software Development :: Libraries",
]
dependencies = [
    "mcp>=1.0",
    "pyyaml>=6.0",
    "click>=8.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-cov"]

[project.scripts]
claude-memory = "claude_memory.cli:main"

[project.urls]
Homepage = "https://github.com/mnardit/claude-memory"
Repository = "https://github.com/mnardit/claude-memory"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**Step 3: Write __init__.py with version and public API**

```python
"""claude-memory — Persistent memory with scope hierarchy for AI agents."""

__version__ = "0.1.0"

from claude_memory.store import MemoryStore
from claude_memory.hierarchy import ScopedView
from claude_memory.config import MemoryConfig, load_config

__all__ = ["MemoryStore", "ScopedView", "MemoryConfig", "load_config", "__version__"]
```

**Step 4: Write LICENSE (MIT) and .gitignore**

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: initial repo scaffolding with pyproject.toml"
```

---

## Task 2: Core Storage Layer (store.py + hierarchy.py + episodes.py + schema.py)

**Files:**
- Copy: `bigboss/lib/memory/store.py` → `claude_memory/store.py`
- Copy: `bigboss/lib/memory/hierarchy.py` → `claude_memory/hierarchy.py`
- Copy: `bigboss/lib/memory/episodes.py` → `claude_memory/episodes.py`
- Copy: `bigboss/lib/memory/schema.py` → `claude_memory/schema.py`
- Copy + adapt: tests

These 4 modules are **fully generic** — zero hardcoded values. Changes needed:

1. Fix imports: `from lib.memory.store import MemoryStore` → `from claude_memory.store import MemoryStore` (only in hierarchy.py)
2. Remove `ticktick_id` from project schema in schema.py (project-specific)
3. Clean up domain-specific slot names in BUILTIN_SCHEMAS (keep structure, generalize names)

**Step 1: Copy modules**

Copy store.py, hierarchy.py, episodes.py verbatim. Fix import in hierarchy.py.

**Step 2: Clean schema.py**

Keep `SlotDef`, `FrameSchema` dataclasses. Keep `BUILTIN_SCHEMAS` but make them generic examples:
- person: name, role, email, phone, timezone, language
- client/project/agency: name, status, description
- Remove ticktick_id, telegram-specific slots

**Step 3: Copy and adapt tests**

Copy `test_memory_store.py`, `test_memory_hierarchy.py`, `test_memory_episodes.py`, `test_memory_schema.py`.
Fix imports: `from lib.memory.X` → `from claude_memory.X`.
Create `tests/conftest.py` with shared `store` fixture.

**Step 4: Run tests, verify all pass**

```bash
cd ~/projects/personal/claude-memory
pip install -e ".[dev]"
pytest tests/ -v
```

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: core storage layer — MemoryStore, ScopedView, episodes, schema"
```

---

## Task 3: YAML-Based Config System

**Files:**
- Create: `claude_memory/config.py` (NEW — replaces bigboss hardcoded config)
- Create: `templates/` directory with default prompt templates
- Create: `tests/test_config.py`

This is the **key refactoring** — replacing hardcoded BOBDO_CLIENTS, TIER0_AGENTS, etc. with a YAML config file.

**Step 1: Write failing tests for config loading**

```python
# tests/test_config.py
def test_load_config_from_yaml(tmp_path):
    """Config loads from YAML file."""
    yaml_content = """
db_path: /tmp/test/frames.db
hierarchy:
  acme:
    children: [client-a, client-b]
tiers:
  0: [sync-bot]
  1: [dashboard]
agent_types:
  system: [dashboard]
  orchestrator: [boss]
"""
    config_file = tmp_path / "memory.yaml"
    config_file.write_text(yaml_content)
    config = load_config(config_file)

    assert config.db_path == Path("/tmp/test/frames.db")
    agent = config.get_agent("client-a")
    assert agent.tier == 2
    assert agent.chain == ["global", "acme", "client-a"]

    agent = config.get_agent("boss")
    assert agent.tier == 3
    assert agent.chain == ["global"]

def test_get_agent_infers_tier_from_hierarchy(tmp_path):
    """Agents under hierarchy parents get tier 2."""
    ...

def test_get_agent_unknown_defaults_to_tier1(tmp_path):
    """Unknown agents default to tier 1 with [global, slug] chain."""
    ...

def test_scope_children(tmp_path):
    """scope_children() returns hierarchy children."""
    ...
```

**Step 2: Implement config.py**

```python
"""YAML-based configuration for claude-memory."""
import yaml
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATHS = [
    Path.cwd() / "memory.yaml",
    Path.home() / ".claude" / "memory" / "memory.yaml",
]
DEFAULT_DB_PATH = Path.home() / ".claude" / "memory" / "frames.db"

@dataclass
class AgentConfig:
    slug: str
    tier: int
    chain: list[str]

@dataclass
class MemoryConfig:
    db_path: Path
    cache_dir: Path
    hierarchy: dict[str, list[str]]    # parent → [children]
    tiers: dict[int, list[str]]        # tier → [slugs]
    agent_types: dict[str, list[str]]  # type → [slugs]
    briefing: dict                     # model, budget, etc.
    templates_dir: Path | None
    vault_dir: Path | None

    def get_agent(self, slug: str) -> AgentConfig:
        """Infer agent config from slug using hierarchy + tiers."""
        # Check orchestrator
        if slug in self.agent_types.get("orchestrator", []):
            return AgentConfig(slug=slug, tier=3, chain=["global"])
        # Check explicit tiers
        for tier, slugs in self.tiers.items():
            if slug in slugs:
                chain = ["global", slug] if tier > 0 else []
                return AgentConfig(slug=slug, tier=tier, chain=chain)
        # Check hierarchy children
        for parent, children in self.hierarchy.items():
            if slug in children:
                return AgentConfig(slug=slug, tier=2,
                                   chain=["global", parent, slug])
        # Check if slug IS a parent
        if slug in self.hierarchy:
            return AgentConfig(slug=slug, tier=2, chain=["global", slug])
        # Default: tier 1
        return AgentConfig(slug=slug, tier=1, chain=["global", slug])

    def scope_children(self, scope: str) -> set[str]:
        """Get children of a scope from hierarchy."""
        return set(self.hierarchy.get(scope, []))

def load_config(path: Path | None = None) -> MemoryConfig:
    """Load config from YAML file. Searches default paths if none given."""
    ...
```

**Step 3: Create default prompt templates as .md files**

```
templates/
├── client.md       # Generic client agent template (no personal names)
├── agency.md       # Generic agency/org template
├── personal.md     # Personal project template
├── topic.md        # Focused topic template
├── system.md       # System utility template
└── orchestrator.md # Orchestrator/meta-agent template
```

Each template uses `{slug}`, `{raw_context}`, `{budget}` placeholders. No hardcoded names.

**Step 4: Run tests**

```bash
pytest tests/test_config.py -v
```

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: YAML-based config system replacing hardcoded constants"
```

---

## Task 4: MCP Bridge + Server (scope enforcement)

**Files:**
- Copy + adapt: `claude_memory/mcp_bridge.py`
- Copy + adapt: `claude_memory/mcp_server.py`
- Copy + adapt: `tests/test_mcp_bridge.py`

**Changes needed:**
1. `mcp_bridge.py`: Replace `_SCOPE_CHILDREN` hardcode with `config.scope_children()`. Accept config in constructor.
2. `mcp_bridge.py`: Replace direct `_conn` access (lines 52-61) with public store API.
3. `mcp_server.py`: Load config from YAML. Parameterize DB path and server name.
4. Tests: Replace `bobdo`/`knestel` fixtures with generic `acme`/`client-a` names.

**Step 1: Write/update failing tests**

Replace all BigBoss-specific agent names in test fixtures with generic names.

**Step 2: Refactor mcp_bridge.py**

```python
class MCPBridge:
    def __init__(self, db_path, default_scope, scope_chain, config=None):
        self._store = MemoryStore(db_path)
        self._scope = default_scope
        self._chain = scope_chain
        self._config = config
        self._enforce = len(scope_chain) > 1

    def _allowed_scopes(self):
        """Scopes this agent can write to."""
        allowed = set(self._chain)
        if self._config:
            local = self._chain[-1] if self._chain else None
            if local:
                allowed |= self._config.scope_children(local)
        return allowed
```

**Step 3: Refactor mcp_server.py**

Load YAML config, get agent by CWD slug, pass config to bridge.

**Step 4: Run tests**

**Step 5: Commit**

```bash
git commit -m "feat: MCP bridge with config-driven scope enforcement"
```

---

## Task 5: Raw Context Assembly

**Files:**
- Copy + adapt: `claude_memory/context.py`
- Copy + adapt: `tests/test_context.py`

**Changes needed:**
1. Replace `VAULT_PROJECTS = Path("/srv/shared/obsidian/projects")` with config-driven path (or None to disable vault tasks)
2. Replace hardcoded `"## Задачи"` section header with configurable value (default: `"## Tasks"`)
3. Accept config parameter in `assemble_context()`

**Step 1: Write/update tests with configurable vault path**

**Step 2: Refactor context.py**

```python
def assemble_context(store, chain, tier, budget=10000,
                     vault_projects_dir=None, task_header="## Tasks"):
```

**Step 3: Run tests**

**Step 4: Commit**

```bash
git commit -m "feat: configurable raw context assembly"
```

---

## Task 6: AI Briefing Generation (context_gen.py)

**Files:**
- Copy + heavy refactor: `claude_memory/context_gen.py`
- Copy + adapt: `tests/test_context_gen.py`

**This is the heaviest refactoring task.** Changes needed:

1. **Remove all hardcoded agent sets** — use config for SYSTEM_AGENTS, ORCHESTRATOR_AGENTS
2. **Externalize prompt templates** — load from `templates/` directory (markdown files with `{slug}`, `{raw_context}`, `{budget}` placeholders)
3. **Pluggable LLM invocation** — not just `claude -p`:
   ```python
   # Default: Claude CLI
   def default_llm_caller(prompt, model, timeout):
       env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
       result = subprocess.run(["claude", "-p", "--model", model], ...)
       return result.stdout.strip()

   # Users can provide their own:
   # config.briefing.llm_callable = my_custom_function
   ```
4. **Replace `generate_all()` dependency on `lib.discover`** — use config's agent list instead:
   ```python
   def generate_all(config, force=False):
       for slug in config.all_agents():
           generate_briefing(slug, config, force=force)
   ```
5. **`_assemble_orchestrator_context`** and **`_assemble_topic_context`** — keep as-is but accept config

**Step 1: Extract prompt templates to markdown files**

Move each template string from PROMPT_TEMPLATES dict to `templates/<type>.md`. Remove personal names (Ortwin, Max Nardit, Bobdo). Keep structure generic:

```markdown
# templates/client.md
You are generating a context briefing for an AI agent.
The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary.
Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — manages a client project.

Raw data from knowledge base:
{raw_context}

Create a structured briefing:

## Key People
For each person: name, role, contact method, key notes.
Only people who work with THIS client.

## Current Tasks
Prioritize: urgent → in progress → can wait.

## Context
Recent events, decisions, agreements.

## Dependencies
Key dependencies between people and projects.

DO NOT include: raw slot data, entity IDs, scope metadata,
people unrelated to this client, completed tasks.
```

**Step 2: Write tests for template loading and LLM pluggability**

```python
def test_load_template_from_file(tmp_path):
    """Templates load from .md files in templates dir."""
    ...

def test_custom_llm_callable():
    """User-provided LLM function is called instead of CLI."""
    ...

def test_generate_briefing_uses_config():
    """generate_briefing reads agent type from config, not hardcode."""
    ...
```

**Step 3: Refactor context_gen.py**

Key changes:
- `get_agent_type()` → reads from `config.agent_types` dict
- `build_prompt()` → loads template from file system
- `call_llm()` → delegates to config's callable (default: claude CLI)
- `generate_all()` → iterates `config.all_agents()` instead of `lib.discover`

**Step 4: Run tests**

**Step 5: Commit**

```bash
git commit -m "feat: AI briefing generation with pluggable templates and LLM"
```

---

## Task 7: CLI (click-based)

**Files:**
- Create: `claude_memory/cli.py` (new, click-based)
- Create: `tests/test_cli.py`

**Commands:**

```bash
claude-memory init                     # Create memory.yaml + DB + install hooks
claude-memory set "Person" role "CTO"  # Set slot
claude-memory get "Person" role        # Get slot
claude-memory search "query"           # Search entities
claude-memory list --type person       # List entities
claude-memory log "Project" "text"     # Add log entry
claude-memory generate                 # Generate AI briefings (all agents)
claude-memory generate --agent slug    # Generate for one agent
claude-memory status                   # Show DB stats, cache status
```

**Step 1: Write CLI tests (click CliRunner)**

**Step 2: Implement with click**

```python
import click
from claude_memory.config import load_config
from claude_memory.store import MemoryStore

@click.group()
@click.option("--config", "-c", type=click.Path(), help="Path to memory.yaml")
@click.pass_context
def main(ctx, config):
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config)

@main.command()
@click.argument("name")
@click.argument("key")
@click.argument("value")
@click.pass_context
def set(ctx, name, key, value):
    """Set a slot value on an entity."""
    ...
```

**Step 3: Run tests**

**Step 4: Commit**

```bash
git commit -m "feat: click-based CLI with init/set/get/search/generate commands"
```

---

## Task 8: Hooks (installable SessionStart + PostToolUse)

**Files:**
- Create: `claude_memory/hooks/session_start.py`
- Create: `claude_memory/hooks/post_tool_use.py`

**These are the Claude Code integration hooks.** The `claude-memory init` command installs them into `~/.claude/settings.json`.

**Step 1: Write session_start.py**

Simplified version of `hooks/memory-context.py`. Reads config from `memory.yaml`, checks cache, falls back to raw context. Outputs JSON `{"additionalContext": "..."}`.

**Step 2: Write post_tool_use.py**

Optional vault regen hook. Only active if `vault.dir` is set in config.

**Step 3: Add `init` command logic**

`claude-memory init` should:
1. Create `memory.yaml` with sensible defaults
2. Create DB directory
3. Create `.mcp.json` in current project
4. Add hooks to `~/.claude/settings.json` (with user confirmation)

**Step 4: Commit**

```bash
git commit -m "feat: installable Claude Code hooks (SessionStart + PostToolUse)"
```

---

## Task 9: Vault Generator (optional output)

**Files:**
- Copy + adapt: `claude_memory/vault_gen.py`
- Copy + adapt: `tests/test_vault_gen.py`

**Changes:**
1. Make vault_dir configurable (from config, not hardcoded)
2. Make git commit/push optional (config flag)
3. Rate limiting stays (but rate file path configurable)

**Step 1: Refactor vault_gen.py to use config**

**Step 2: Update tests**

**Step 3: Commit**

```bash
git commit -m "feat: configurable vault output generator"
```

---

## Task 10: Dedup Utilities

**Files:**
- Extract from: `bigboss/dedup_people.py` → `claude_memory/dedup.py`
- Copy + adapt: `tests/test_dedup.py`

**Extract the generic parts:**
- Name parsing (first/last, transliteration)
- Similarity scoring
- Candidate finding
- Merge logic

**Leave out:** Interactive CLI (BigBoss-specific). Provide API only.

**Step 1: Extract dedup functions**

**Step 2: Adapt tests**

**Step 3: Commit**

```bash
git commit -m "feat: entity deduplication utilities"
```

---

## Task 11: Examples + Documentation

**Files:**
- Create: `examples/memory.yaml`
- Create: `examples/quickstart.py`
- Create: `examples/multi_agent_setup/`
- Create: `README.md`

**Step 1: Write example config**

A realistic `memory.yaml` showing a marketing agency setup (generic names).

**Step 2: Write quickstart.py**

```python
"""Minimal claude-memory usage example."""
from claude_memory import MemoryStore

store = MemoryStore("memory.db")
eid = store.create_entity("Alice", "person")
store.set_slot(eid, "role", "Engineering Lead")
store.add_observation(eid, "Prefers async communication")
print(store.search("Alice"))
store.close()
```

**Step 3: Write README.md**

Architecture diagram (ASCII art), features list, quickstart, configuration reference, links to examples.

**Step 4: Commit**

```bash
git commit -m "docs: README, examples, and quickstart guide"
```

---

## Task 12: BigBoss Migration

**Files:**
- Modify: `bigboss/lib/memory/` → delete (replaced by pip package)
- Create: `bigboss/memory.yaml` (our specific config)
- Create: `bigboss/templates/` (our specific prompt templates)
- Modify: all imports across bigboss

**Step 1: Create bigboss-specific memory.yaml**

```yaml
db_path: ~/.claude/memory/frames.db
cache_dir: ~/.claude/memory/context_cache

hierarchy:
  bobdo:
    children: [knestel, gurgl, wkv, hotel-award, budo7, tischbein,
               zimm, luna, finance-li, xiwine, etl-bodensee]
  sekta:
    children: [1cvpn, camrelay]

tiers:
  0: [gmail-sync, telegram-sync, google-ads-sync, gsc-sync, ga4-sync, asset-inbox]
  1: [ticktick-agent, edu-tutor, personal-site, beetroot,
      agency-dashboard, thailand-admin, alter-ego]

agent_types:
  system: [agency-dashboard, ticktick-agent]
  orchestrator: [bigboss]

briefing:
  model: opus
  cache_max_age: 86400
  raw_budget: 50000
  output_budget: 8000
  timeout: 300

vault:
  dir: /srv/shared/obsidian
  auto_commit: true
  task_header: "## Задачи"
```

**Step 2: Copy our prompt templates to bigboss/templates/**

Keep the Russian/German-specific templates with Bobdo, Ortwin, Max Nardit references.

**Step 3: pip install -e ~/projects/personal/claude-memory/**

**Step 4: Replace imports**

Global find/replace across bigboss:
- `from lib.memory.store import MemoryStore` → `from claude_memory.store import MemoryStore`
- `from lib.memory.config import ...` → `from claude_memory.config import ...`
- etc. for all modules

Update: `generate_contexts.py`, `hooks/memory-context.py`, `hooks/vault-regen.py`, `nightly.py`, `maintenance.py`, `new_project.py`, `dedup_people.py`, `topic.py`, `lib/topic.py`, `sync_tasks.py`

**Step 5: Delete bigboss/lib/memory/**

**Step 6: Run all bigboss tests to verify**

```bash
cd ~/projects/bigboss
pytest tests/ -v
```

**Step 7: Update .mcp.json in all projects**

Change MCP server path from `bigboss/lib/memory/mcp_server.py` to the package's entry point.

**Step 8: Commit bigboss changes**

```bash
git add -A
git commit -m "refactor: migrate to claude-memory package, remove lib/memory/"
```

---

## Task 13: GitHub Release

**Files:**
- Push: claude-memory repo to GitHub
- Create: GitHub release v0.1.0

**Step 1: Create GitHub repo**

```bash
gh repo create mnardit/claude-memory --public --description "Persistent memory with scope hierarchy for AI agents in Claude Code"
```

**Step 2: Push**

```bash
cd ~/projects/personal/claude-memory
git remote add origin git@github.com:mnardit/claude-memory.git
git push -u origin main
```

**Step 3: Create release**

```bash
gh release create v0.1.0 --title "v0.1.0 — Initial Release" --notes "..."
```

**Step 4: Verify pip install works**

```bash
pip install git+https://github.com/mnardit/claude-memory.git
```

---

## Dependency Order

```
Task 1  (scaffolding)
  ↓
Task 2  (core: store, hierarchy, episodes, schema)
  ↓
Task 3  (config system) ← KEY — everything after depends on this
  ↓
  ├── Task 4  (MCP bridge + server)
  ├── Task 5  (raw context assembly)
  ├── Task 9  (vault generator)
  └── Task 10 (dedup)
  ↓
Task 6  (AI briefing gen) ← depends on 3, 5
  ↓
Task 7  (CLI) ← depends on 3, 6
  ↓
Task 8  (hooks) ← depends on 3, 5, 6
  ↓
Task 11 (docs + examples) ← depends on all above
  ↓
Task 12 (bigboss migration) ← depends on all above
  ↓
Task 13 (GitHub release)
```

**Parallelizable:** Tasks 4, 5, 9, 10 can run in parallel after Task 3.

---

## Estimated Effort

| Task | Effort | Notes |
|------|--------|-------|
| 1. Scaffolding | 30 min | Boilerplate |
| 2. Core storage | 1-2 hrs | Mostly copy, fix imports |
| 3. Config system | 3-4 hrs | Key refactoring, YAML loader |
| 4. MCP bridge | 2-3 hrs | Scope enforcement refactoring |
| 5. Context assembly | 1-2 hrs | Minor config injection |
| 6. AI briefing gen | 4-5 hrs | Heaviest — templates, pluggable LLM |
| 7. CLI | 2-3 hrs | Click rewrite |
| 8. Hooks | 1-2 hrs | Mostly cleanup |
| 9. Vault gen | 1 hr | Config injection |
| 10. Dedup | 1-2 hrs | Extract API |
| 11. Docs + examples | 3-4 hrs | README, examples, diagrams |
| 12. BigBoss migration | 2-3 hrs | Import replacement, testing |
| 13. GitHub release | 30 min | Push + release |
| **Total** | **~24-32 hrs** | **~3-4 days focused work** |

---

## Risk Mitigation

1. **Breaking bigboss during migration** — Task 12 is last. Keep `lib/memory/` in bigboss until claude-memory passes all tests independently. Editable install means both work side-by-side.

2. **MCP server path changes** — All 30+ projects have `.mcp.json` pointing to `bigboss/lib/memory/mcp_server.py`. Migration script needed (Task 12 Step 7).

3. **Timer/hook paths** — `generate_contexts.py`, hooks reference `lib.memory`. After migration, update all paths.

4. **Test parity** — Every task includes copying + adapting existing tests. Target: same coverage in new package as in bigboss (147 tests).
