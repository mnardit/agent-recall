# claude-memory

Persistent memory with scope hierarchy for AI agents in Claude Code.

SQLite-backed knowledge graph with scoped slots, observations, relations, and AI-generated briefings. Drop-in MCP server for Claude Code agents.

## Features

- **Scoped memory store** — SQLite with bitemporal slots, observations, relations, log entries, documents
- **Scope hierarchy** — chain-based inheritance (`global → agency → client`), local overrides parent
- **MCP server** — FastMCP stdio server compatible with Claude Code's `.mcp.json`
- **Scope enforcement** — agents can only write to entities in their scope tree
- **AI briefings** — LLM summarizes raw context into structured agent briefings (pluggable LLM)
- **Vault generation** — optional Obsidian-compatible markdown output
- **Entity deduplication** — fuzzy name matching and merge utilities
- **CLI** — `claude-memory` command with init, search, generate, and more
- **Hooks** — SessionStart (inject context) and PostToolUse (auto-regen vault)

## Install

```bash
pip install claude-memory
```

## Quick Start

### 1. Initialize

```bash
claude-memory init
```

Creates the SQLite database at `~/.claude/memory/frames.db`.

### 2. Configure

Create `memory.yaml` in your project root or `~/.claude/memory/memory.yaml`:

```yaml
db_path: ~/.claude/memory/frames.db
cache_dir: ~/.claude/memory/context_cache

hierarchy:
  acme-agency:
    - client-a
    - client-b

tiers:
  0: [infra-bot]        # No context injection
  2: [acme-agency, client-a, client-b]

agent_types:
  orchestrator: [bigboss]
  system: [dashboard]

briefing:
  model: opus
  timeout: 300
```

### 3. Add MCP Server

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "claude_memory.mcp_server"]
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
      { "command": "claude-memory-session-start" }
    ],
    "PostToolUse": [
      { "command": "claude-memory-post-tool-use" }
    ]
  }
}
```

The SessionStart hook injects agent context (cached AI briefing or raw context) at startup.
The PostToolUse hook regenerates vault files after MCP memory writes.

### 5. Generate AI Briefings

```bash
# Single agent
claude-memory generate my-agent --force

# All agents
claude-memory refresh --force
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
from claude_memory import MemoryStore, ScopedView, MemoryConfig, load_config

# Direct store access
store = MemoryStore("memory.db")
eid = store.resolve_entity("Alice", "person")
store.set_slot(eid, "role", "Engineer", scope="acme")
store.add_observation(eid, "Expert in Python", scope="acme")

# Scoped view (chain-based inheritance)
view = ScopedView(store, ["global", "acme", "project-x"])
entity = view.get_entity("Alice")
print(entity["slots"])  # Merged from all scopes in chain

store.close()
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

Raw context from the knowledge graph is summarized by an LLM into structured briefings.

- 6 built-in prompt templates: client, agency, personal, topic, system, orchestrator
- Templates loadable from `.md` files (override builtins)
- Pluggable LLM caller — default uses `claude -p` CLI, pass any `(prompt, model, timeout) -> str` callable
- Cached briefings with configurable max age (default 24h)

## Development

```bash
git clone https://github.com/mnardit/claude-memory.git
cd claude-memory
pip install -e ".[dev]"
pytest
```

## License

MIT
