# agent-memory

Persistent memory with scope hierarchy for AI agents in Claude Code.

SQLite-backed knowledge graph with scoped slots, observations, relations, and AI-generated briefings. Drop-in MCP server for Claude Code agents.

## Features

- **Scoped memory store** — SQLite with bitemporal slots, observations, relations, log entries, documents
- **Scope hierarchy** — chain-based inheritance (`global → agency → client`), local overrides parent
- **MCP server** — FastMCP stdio server compatible with Claude Code's `.mcp.json`
- **Scope enforcement** — agents can only write to entities in their scope tree
- **AI briefings** — LLM summarizes raw context into structured agent briefings (pluggable LLM)
- **Per-agent config** — model, template, enabled/disabled, context files per agent
- **Adaptive caching** — `.stale` markers with cross-agent invalidation triggers
- **Status API** — query cache freshness, generation logs, token usage per agent
- **Vault generation** — optional Obsidian-compatible markdown output
- **Entity deduplication** — fuzzy name matching and atomic merge
- **CLI** — `agent-memory` command with init, search, generate, status, and more
- **Hooks** — SessionStart (inject context) and PostToolUse (auto-regen vault, cache invalidation)

## Install

```bash
pip install agent-memory
```

For MCP server support:

```bash
pip install 'agent-memory[mcp]'
```

## Quick Start

### 1. Initialize

```bash
agent-memory init
```

Creates the SQLite database at `~/.claude/memory/frames.db`.

### 2. Configure

Create `memory.yaml` in your project root or `~/.claude/memory/memory.yaml`:

```yaml
db_path: ~/.claude/memory/frames.db
cache_dir: ~/.claude/memory/context_cache

hierarchy:
  acme-agency:
    children: [client-a, client-b]

tiers:
  0: [infra-bot]        # No context injection
  2: [acme-agency, client-a, client-b]

agent_types:
  orchestrator: [bigboss]
  system: [dashboard]

briefing:
  model: opus
  timeout: 300
  adaptive: true         # Enable stale markers + auto-regen
  min_cache_age: 1800    # Don't regen more than every 30 min

# Per-agent overrides
agents:
  bigboss:
    model: opus
    timeout: 300
    output_budget: 12000
  dashboard:
    model: haiku
    template: system
    context_files:
      - ~/projects/dashboard/README.md
    context_budget: 5000
  retired-agent:
    enabled: false

vault:
  dir: /path/to/obsidian/vault
  task_header: "## Tasks"
  auto_commit: true
```

### 3. Add MCP Server

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_memory.mcp_server"]
    }
  }
}
```

Now your Claude Code agents can use `create_entities`, `add_observations`, `search_nodes`, etc.

### 4. Install Hooks

Add to your Claude Code settings (`.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [
      { "command": "agent-memory-session-start" }
    ],
    "PostToolUse": [
      { "command": "agent-memory-post-tool-use" }
    ]
  }
}
```

The SessionStart hook injects agent context (cached AI briefing or raw context) at startup.
The PostToolUse hook regenerates vault files and invalidates affected caches after MCP memory writes.

### 5. Generate AI Briefings

```bash
# Single agent
agent-memory generate my-agent --force

# All agents
agent-memory refresh --force
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize database |
| `set <entity> <type> <key> <value>` | Set a slot value |
| `get <entity> <key>` | Get a slot value |
| `entity <name>` | Show entity details |
| `list [--type TYPE]` | List entities |
| `search <query>` | Search by name, slot, or observation |
| `history <entity> <key>` | Show bitemporal slot history |
| `log <entity> <text>` | Add a log entry |
| `logs <entity>` | Show log entries |
| `generate <slug>` | Generate AI briefing for one agent |
| `refresh` | Refresh all agent briefings |
| `status` | Show database stats |

All commands accept `--db` and `--config` overrides.

## Python API

```python
from agent_memory import MemoryStore, ScopedView, MemoryConfig, load_config

# Direct store access
with MemoryStore("memory.db") as store:
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "Engineer", scope="acme")
    store.add_observation(eid, "Expert in Python", scope="acme")

    # Scoped view (chain-based inheritance)
    view = ScopedView(store, ["global", "acme", "project-x"])
    entity = view.get_entity("Alice")
    print(entity["slots"])  # Merged from all scopes in chain
```

### AI Briefing API

```python
from agent_memory import generate_briefing, get_agent_status, LLMResult

# Generate a briefing
path = generate_briefing("my-agent", force=True)

# Custom LLM caller with token tracking
def my_llm(prompt: str, model: str, timeout: int) -> LLMResult:
    result = call_my_api(prompt, model)
    return LLMResult(
        text=result.text,
        input_tokens=result.usage.input,
        output_tokens=result.usage.output,
    )

generate_briefing("my-agent", llm_caller=my_llm, force=True)

# Query status
status = get_agent_status("my-agent")
# {"has_cache": True, "is_fresh": True, "model": "opus", "age_seconds": 3600, ...}
```

## Scope Hierarchy

Memory is organized in scope chains. Each agent sees data from its chain:

```
global → acme-agency → client-a
                     → client-b
       → personal → side-project
```

- **Slots** are scoped: same key can have different values per scope
- **Local overrides parent**: `client-a` scope value wins over `acme-agency`
- **Observations** have scope: agents only see observations in their chain
- **Enforcement**: MCP bridge prevents cross-scope writes

## AI Briefings

Raw context from the knowledge graph is summarized by an LLM into structured agent briefings.

- 6 built-in prompt templates: client, agency, personal, topic, system, orchestrator
- Templates loadable from `.md` files (override builtins)
- Per-agent template override: use a different builtin type or inline custom text
- Pluggable LLM caller — default uses `claude -p` CLI, pass any `(prompt, model, timeout) -> str | LLMResult` callable
- Cached briefings with configurable max age (default 24h)
- Adaptive cache invalidation with `.stale` markers and cross-agent triggers
- Generation logging with rotation (last 10 entries per agent, with token counts)
- `LLMResult` dataclass for structured responses with optional token tracking

### Context Assembly Types

| Type | Used For | Description |
|------|----------|-------------|
| Standard | client, agency, personal, system | Scope-chain based, filtered by chain |
| Orchestrator | orchestrator agents | All scopes, all entities — bird's eye view |
| Topic | topic sub-sessions | Topic entity + observations-aware scoping |

## Per-Agent Configuration

The `agents` section in `memory.yaml` allows per-agent overrides:

| Key | Type | Description |
|-----|------|-------------|
| `model` | string | Override LLM model for this agent |
| `timeout` | int | Override LLM timeout |
| `output_budget` | int | Target output size in characters |
| `template` | string | Builtin type name or inline template text |
| `enabled` | bool | Disable briefing generation (default: true) |
| `context_files` | list | Extra files to include in context |
| `context_budget` | int | Max chars for context files (default: 3000) |
| `extra_context` | string | Static text appended to raw context |
| `adaptive` | bool | Per-agent adaptive cache override |
| `min_cache_age` | int | Min seconds between regenerations |

## Development

```bash
git clone https://github.com/mnardit/agent-memory.git
cd agent-memory
pip install -e ".[dev]"
pytest
```

259 tests covering store, config, hierarchy, context assembly, AI briefings, vault generation, hooks, dedup, and MCP bridge.

## License

MIT
