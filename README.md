# agent-recall

[![Tests](https://github.com/mnardit/agent-recall/actions/workflows/tests.yml/badge.svg)](https://github.com/mnardit/agent-recall/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-recall)](https://pypi.org/project/agent-recall/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/agent-recall/)

**Persistent memory for AI coding agents.** Your agent forgets everything between sessions — names, decisions, preferences, context. agent-recall fixes this.

Built from production: extracted from a real system running **30+ concurrent AI agents** at a digital agency. Not a prototype — every feature exists because something broke in production.

```
Before:  "Who is Alice?" (every single session)
After:   Agent starts with: "Alice — Lead Engineer at Acme, prefers async,
         last discussed the API migration on Feb 12"
```

**MCP-native** — designed for MCP-compatible clients. Tested configs included for Claude Code, Cursor, Windsurf, and Cline. Battle-tested daily with Claude Code (30+ agents in production). [PRs and issue reports welcome!](https://github.com/mnardit/agent-recall/issues)

### Why agent-recall?

Other memory solutions exist (Mem0, Zep/Graphiti, LangMem). Here's what makes agent-recall different:

- **Scope hierarchy** — not flat memory. The same person can have different roles in different projects. agent-recall is built around scope chains with inheritance — designed for agents working across multiple clients, projects, and nested contexts.
- **AI briefings** — raw data dumps don't work. agent-recall uses an LLM to summarize hundreds of facts into structured, actionable context injected at session start.
- **Local-first** — single SQLite file. No cloud, no vector DB, no Docker, no Neo4j. Your data stays on your machine.
- **MCP-native** — 9 memory tools with proactive-saving instructions. Tested configs for Claude Code, Cursor, Windsurf, and Cline.
- **Bitemporal** — old values are archived, not deleted. Query what was true at any point in time.
- **Minimal dependencies** — just `pyyaml` + `click`. MCP and Anthropic SDK are optional extras.

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                         SESSION 1                                   │
│                                                                     │
│  You: "Alice from Acme called. She wants the API done by Friday."   │
│                           │                                         │
│                           ▼                                         │
│  Agent saves automatically via MCP tools:                           │
│    create_entities: Alice (person), Acme (client)                   │
│    add_observations: "wants API done by Friday"                     │
│    create_relations: Alice → works_at → Acme                        │
│                           │                                         │
│                           ▼                                         │
│  Stored in local SQLite ─────► ~/.agent-recall/frames.db            │
└─────────────────────────────────────────────────────────────────────┘
                            │
                     (session ends)
                            │
┌─────────────────────────────────────────────────────────────────────┐
│                         SESSION 2                                   │
│                                                                     │
│  Agent starts and receives a briefing:                              │
│    "Alice (Lead Engineer, Acme) — wants API done by Friday.         │
│     Acme is a client. Last discussed Feb 12."                       │
│                           │                                         │
│                           ▼                                         │
│  Agent already knows who Alice is, what's urgent, and what to do.   │
└─────────────────────────────────────────────────────────────────────┘
```

**Why does the agent save facts automatically?** The MCP server includes behavioral instructions that tell the agent to proactively save people, decisions, and context as it encounters them. No special prompting needed — the agent receives these instructions when it connects to the memory server.

---

## Setup

### Option A: Claude Code Plugin (recommended)

```bash
pip install 'agent-recall[mcp]'
agent-recall init
```

Then in Claude Code:

```
/plugins add mnardit/agent-recall
```

This installs the MCP server and hooks automatically — no manual config needed.

### Option B: Manual setup

#### Step 1: Install

```bash
pip install 'agent-recall[mcp]'
agent-recall init
```

This creates the SQLite database at `~/.agent-recall/frames.db`.

> `agent-recall[mcp]` installs with MCP server support. Use `pip install agent-recall` if you only need the Python API/CLI.

#### Step 2: Add MCP server to your editor

This gives your agent the memory tools (`create_entities`, `add_observations`, `search_nodes`, etc.) and the instructions to use them proactively.

<details open>
<summary><strong>Claude Code</strong> ✅ Production-tested</summary>

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary><strong>Cursor</strong></summary>

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary><strong>Windsurf</strong></summary>

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary><strong>Cline</strong></summary>

Add to `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
```
</details>

#### Step 3: (Claude Code) Add hooks for automatic context injection

Hooks make the agent receive its memory briefing at the start of every session, and keep caches fresh after writes. **This step is optional but strongly recommended for Claude Code users.**

Add to `.claude/settings.json` (project or global):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "agent-recall-session-start"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "mcp__memory__.*",
        "hooks": [
          {
            "type": "command",
            "command": "agent-recall-post-tool-use"
          }
        ]
      }
    ]
  }
}
```

| Hook | What it does |
|------|-------------|
| `SessionStart` | Injects AI briefing (or raw context) into the agent's system prompt when a session starts |
| `PostToolUse` | After the agent writes to memory (matched by `mcp__memory__.*`), invalidates stale caches and regenerates vault files |

> **Other editors:** Hooks are Claude Code-specific. For other clients, use the [CLI](#cli) (`agent-recall generate`) or [Python API](#python-api) to generate and serve briefings.

#### Step 4: Verify it works

Start a new session with your agent and look for:
- The agent should have memory tools listed (e.g., `create_entities`, `search_nodes`)
- If hooks are set up, the agent shows a "Memory is empty" message on first run
- Mention a person or make a decision — the agent should save it automatically
- Start another session — the agent should know about the person/decision

---

## Quick Start

Get a working memory system in 3 minutes:

```bash
# Install
pip install 'agent-recall[mcp]'

# Initialize the database
agent-recall init

# Set your agent identity
export AGENT_RECALL_SLUG=my-project

# Add to your .mcp.json (Claude Code example)
cat > .mcp.json << 'EOF'
{
  "mcpServers": {
    "memory": {
      "command": "python3",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
EOF

# Start Claude Code — memory tools are now available!
# The agent can create_entities, add_observations, search_nodes, etc.

# Generate an AI briefing from stored knowledge
agent-recall generate my-project
```

That's it! Your agent now has persistent memory across sessions. The MCP server
exposes 9 tools for reading and writing the knowledge graph. See [Configuration](#configuration)
for customization options.

---

## What Happens Under the Hood

Here's the full lifecycle:

```
1. CONNECT
   Agent connects to MCP server
   └─► Server sends instructions: "Proactively save people, decisions, facts..."
   └─► Agent receives 9 memory tools with descriptions explaining when to use each

2. SAVE (during conversation)
   Agent encounters important information
   └─► search_nodes("Alice")           — check if entity exists
   └─► create_entities([{...}])        — create if new
   └─► add_observations([{...}])       — add facts to existing entity
   └─► create_relations([{...}])       — link entities together
   All stored in ~/.agent-recall/frames.db (SQLite, scoped per project)

3. NOTIFY (PostToolUse hook, Claude Code only)
   After each memory write
   └─► Marks affected agent caches as stale
   └─► Regenerates Obsidian vault files (if configured)

4. BRIEFING (SessionStart hook or CLI)
   Next session starts
   └─► Reads cached AI briefing (if fresh)
   └─► Or assembles raw context from database
   └─► Or generates new briefing via LLM (if stale + adaptive mode)
   └─► Injects into agent's system prompt

5. AGENT KNOWS
   Agent starts with structured context:
   └─► Key people, their roles, preferences
   └─► Current tasks, blockers, deadlines
   └─► Recent decisions and their rationale
```

---

## Key Concepts

| Concept | Description |
|---------|-------------|
| **Entity** | A named thing: person, client, project. Has a type and unique name. |
| **Slot** | A key-value pair on an entity (e.g., `role: "Engineer"`). Scoped and bitemporal — old values are archived, not deleted. |
| **Observation** | Free-text fact attached to an entity (e.g., "Prefers async communication"). Scoped. |
| **Relation** | Directed link between two entities (e.g., Alice —works_at→ Acme). |
| **Scope** | Namespace for data isolation. Slots and observations belong to a scope (e.g., `"global"`, `"acme"`, `"proj-a"`). |
| **Scope chain** | Ordered list of scopes from general to specific: `["global", "acme", "proj-a"]`. Local overrides parent for the same slot. |
| **Tier** | Agent importance level: 0 = no context, 1 = minimal, 2 = full (default), 3 = orchestrator (sees everything). |
| **Briefing** | AI-generated summary of raw memory data, injected into agent's system prompt at startup. Cached and invalidated adaptively. |

---

## Features

### Scoped Memory

Not a flat key-value store. Memory is **scoped** — the same person can have different roles in different projects:

```
Alice:
  role (global)     = "Engineer"
  role (acme)       = "Lead Engineer"    ← Agent working on Acme sees this
  role (beta-corp)  = "Consultant"       ← Agent working on Beta sees this
```

Scoping keeps context clean across projects and prevents data from leaking between workstreams.

### AI Briefings

Raw data dumps don't work. Thousands of facts is noise, not context.

agent-recall uses an LLM to **summarize** what matters into a structured briefing:

```
Raw (what's in the database):
  147 slots across 34 entities, 89 observations, 23 relations...

Briefing (what the agent actually sees):
  ## Key People
  - Alice (Lead Engineer, Acme) — prefers async, owns the API migration
  - Bob (PM) — on vacation until Feb 20

  ## Current Tasks
  - API migration: blocked on auth module (Alice working on it)
  - Dashboard redesign: waiting for Bob's review

  ## Recent Decisions
  - Team agreed to use GraphQL on Feb 10 call
  - Next client meeting: Feb 19
```

Generate briefings via CLI (`agent-recall generate my-agent`) or let the SessionStart hook handle it automatically.

### Multi-Agent Ready

Built for systems with multiple agents sharing one knowledge base but seeing different slices:

```
global → acme-agency → client-a      (client-a sees: global + acme + client-a)
                     → client-b      (client-b sees: global + acme + client-b)
       → personal → side-project     (side-project sees: global + personal + side-project)
```

Each agent reads and writes within its scope chain. The MCP server enforces this automatically.

### Adaptive Cache

When one agent saves new facts, caches for affected agents are marked stale. Next time those agents start a session, their briefings regenerate automatically.

---

## Configuration

For a single agent with defaults, no config file is needed. By default, agent-recall auto-discovers project files (`CLAUDE.md`, `README.md`, `.cursorrules`, `.windsurfrules`) in the current directory and includes them in the data sent to the LLM for briefing generation. This means new agents get useful briefings from day one, even with an empty database. Disable with `auto_discover: false` in the briefing config.

For multiple agents or custom settings, create `memory.yaml` in your project root or `~/.agent-recall/memory.yaml`:

```yaml
# Database location (default: ~/.agent-recall/frames.db)
db_path: ~/.agent-recall/frames.db
cache_dir: ~/.agent-recall/context_cache

# Scope hierarchy — which agents see which data
hierarchy:
  acme-agency:
    - client-a
    - client-b

# Tier 0 = no context injection, Tier 2 = full
tiers:
  0: [infra-bot]
  2: [acme-agency, client-a, client-b]

# AI briefing settings
briefing:
  backend: cli          # "cli" = claude -p (free on subscription), "api" = Anthropic SDK (needs API key)
  model: opus           # LLM model for generating briefings
  timeout: 300          # LLM timeout in seconds
  adaptive: true        # Auto-regenerate stale caches
  min_cache_age: 1800   # Minimum 30 min between regenerations

# Per-agent overrides
agents:
  coordinator:
    model: opus
    output_budget: 12000
  dashboard:
    model: haiku
    template: system
```

<details>
<summary><strong>All per-agent options</strong></summary>

| Key | Type | Description |
|-----|------|-------------|
| `model` | string | LLM model for this agent's briefings |
| `timeout` | int | LLM timeout in seconds |
| `output_budget` | int | Target output size in characters |
| `template` | string | Builtin type name or inline text |
| `enabled` | bool | Disable briefing generation (default: true) |
| `context_files` | list | Extra files to include in context |
| `context_budget` | int | Max chars for context files (default: 8000) |
| `extra_context` | string | Static text appended to raw context |
| `adaptive` | bool | Per-agent adaptive cache override |
| `min_cache_age` | int | Min seconds between regenerations |

</details>

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `AGENT_RECALL_SLUG` | Explicit agent identifier (defaults to current directory name) |

---

## LLM Backend

AI briefings need an LLM to generate summaries. Two built-in backends:

| Backend | Config | Install | Cost | Notes |
|---------|--------|---------|------|-------|
| `cli` (default) | `backend: cli` | Claude Code installed | Free on Claude Pro/Team subscription | Creates a session file per call |
| `api` | `backend: api` | `pip install 'agent-recall[api]'` | Pay per token | Clean, no side effects, needs `ANTHROPIC_API_KEY` |

Switch in `memory.yaml`:
```yaml
briefing:
  backend: api    # uses Anthropic SDK instead of claude CLI
  model: opus
```

### Bring Your Own LLM

For other providers, pass a callable matching `(prompt, model, timeout) -> str`:

```python
from agent_recall import generate_briefing, LLMResult

def my_llm(prompt: str, model: str, timeout: int) -> LLMResult:
    result = call_my_api(prompt, model)
    return LLMResult(text=result.text, input_tokens=result.usage.input,
                     output_tokens=result.usage.output)

generate_briefing("my-agent", llm_caller=my_llm, force=True)
```

Full examples: [OpenAI](examples/llm_openai.py) | [Anthropic SDK](examples/llm_anthropic.py) | [Ollama](examples/llm_ollama.py)

By default, briefing generation uses the `claude` CLI (`claude -p --model <model>`). If you don't use Claude, pass your own `llm_caller`.

---

## CLI

```bash
agent-recall init                          # Create database
agent-recall status                        # Database stats
agent-recall set Alice role Engineer       # Set slot (existing entity)
agent-recall set Alice role Engineer --type person  # Create new entity + set slot
agent-recall get Alice role                # Get slot value
agent-recall entity Alice                  # Show entity details + observations
agent-recall entity Alice --scope global --scope acme  # Scoped slot resolution
agent-recall entity Alice --json           # JSON output
agent-recall list                          # List all entities
agent-recall list --type person            # Filter by type
agent-recall search "engineer"             # Search entities
agent-recall history Alice role            # Bitemporal slot history
agent-recall log Alice "Joined the team"   # Add log entry
agent-recall log Alice "Update" --author human  # Log with custom author
agent-recall logs Alice                    # Show log entries
agent-recall generate my-agent --force     # Generate AI briefing
agent-recall refresh --force               # Refresh all briefings
agent-recall rename-scope old-name new-name  # Migrate data between scopes
```

## Python API

```python
from agent_recall import MemoryStore, ScopedView

with MemoryStore() as store:
    # Create entities
    alice = store.resolve_entity("Alice", "person")
    acme = store.resolve_entity("Acme Corp", "client")

    # Scoped slots — same key, different values per scope
    store.set_slot(alice, "role", "Engineer", scope="global")
    store.set_slot(alice, "role", "Lead Engineer", scope="acme")
    store.add_observation(alice, "Prefers async communication", scope="acme")
    store.add_relation(alice, acme, "works_at")

    # Scoped view — local overrides parent
    view = ScopedView(store, ["global", "acme"])
    entity = view.get_entity("Alice")
    print(entity["slots"]["role"])  # "Lead Engineer" (acme overrides global)

    # Search across all entities
    results = store.search("engineer")
    print(results)  # [{"name": "Alice", "type": "person", ...}]

    # Bitemporal history — see all past values
    store.set_slot(alice, "role", "Staff Engineer", scope="acme")
    history = store.get_slot_history(alice, "role")
    # Returns: all values with valid_from/valid_to timestamps

    # Atomic operations
    with store.transaction():
        bob = store.resolve_entity("Bob", "person")
        store.set_slot(bob, "role", "PM")
        store.add_relation(bob, acme, "works_at")
```

See [`examples/quickstart.py`](examples/quickstart.py) for a runnable version.

---

## Troubleshooting

<details>
<summary><strong>Agent doesn't save facts automatically</strong></summary>

The MCP server includes instructions that tell the agent to proactively save. If it's not working:
1. Verify the MCP server is connected: your agent should list `create_entities`, `search_nodes` etc. as available tools
2. Check the server is running: `python3 -m agent_recall.mcp_server` should start without errors
3. Some agents need a nudge — mention "save this to memory" in your prompt
</details>

<details>
<summary><strong>Briefings are empty or generation fails</strong></summary>

1. Check you have data: `agent-recall status` should show entities
2. The default `cli` backend needs Claude Code installed (`claude -p`). If you don't use Claude, configure the `api` backend with `ANTHROPIC_API_KEY`, or pass your own `llm_caller` (see [LLM Backend](#llm-backend))
3. Run with force: `agent-recall generate my-agent --force`
4. Check cache dir exists: `ls ~/.agent-recall/context_cache/`
</details>

<details>
<summary><strong>Windows: <code>python3</code> not found</strong></summary>

On Windows, Python is usually `python` not `python3`. Update your MCP config:
```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "agent_recall.mcp_server"]
    }
  }
}
```
</details>

---

## Born in Production

agent-recall was extracted from a live system managing real client projects at a digital agency — 30+ agents, 15+ clients, hundreds of scoped facts.

Why specific features exist:
- **Scope isolation** — two agents wrote conflicting data to the same entity
- **Adaptive caching** — briefings went stale during busy hours
- **AI summaries** — agents couldn't make sense of raw data dumps with hundreds of entries
- **Proactive saving instructions** — agents ignored memory tools until explicitly told to use them
- **Bitemporal slots** — needed to track what was true *when*, not just what's true now

### agent-recall vs Claude Code Auto-Memory

| Feature | agent-recall | CC Auto-Memory |
|---------|-------------|----------------|
| **Storage** | SQLite knowledge graph | Markdown files |
| **Structure** | Entities, relations, observations, scopes | Flat key-value in MEMORY.md |
| **Multi-agent** | Shared DB with scope isolation | Per-project, single-agent |
| **Search** | Full-text across entities, slots, observations | File-based |
| **AI Briefings** | LLM-summarized context at session start | Raw memory injected |
| **Scope control** | Hierarchical (global → parent → child) | Per-project directory |
| **Best for** | Teams of agents, complex projects | Single agent, simple projects |

They can work together: use auto-memory for quick preferences and agent-recall
for structured knowledge that spans agents and sessions.

---

## Development

```bash
git clone https://github.com/mnardit/agent-recall.git
cd agent-recall
pip install -e ".[dev]"
pytest
```

388 tests covering store, config, hierarchy, context assembly, AI briefings, vault generation, hooks, dedup, MCP bridge, FTS search, and migrations.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT
