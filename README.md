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

Works with **Claude Code**, **Cursor**, **Windsurf**, **Cline** — any MCP-compatible client.

### Why agent-recall?

Other memory solutions exist (Mem0, Zep, LangGraph checkpointing). Here's what makes this different:

| | agent-recall | Mem0 | Zep | LangGraph |
|---|---|---|---|---|
| **Deployment** | Local SQLite | Cloud-first | Self-hosted (Neo4j) | Postgres/SQLite |
| **Multi-tenant** | Scope hierarchy | User IDs | Org-based | Thread-based |
| **AI briefings** | Built-in | No | No | No |
| **MCP integration** | Native | No | No | No |
| **Temporal queries** | Bitemporal slots | Versioned | Valid at/invalid at | Checkpoints |
| **Open source** | MIT | Limited | Yes | Apache 2.0 |
| **Cost** | Free | Free tier + paid | Free | Free |

**In short:** Local-first. Your data stays on your machine. Multi-tenant scope hierarchy, not just user IDs — built for agencies and teams managing multiple projects. AI briefings that summarize hundreds of facts into what actually matters. MCP-native — works with any editor that supports MCP.

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

### Step 1: Install

```bash
pip install 'agent-recall[mcp]'
agent-recall init
```

This creates the SQLite database at `~/.agent-recall/frames.db`.

> `agent-recall[mcp]` installs with MCP server support. Use `pip install agent-recall` if you only need the Python API/CLI.

### Step 2: Add MCP server to your editor

This gives your agent the memory tools (`create_entities`, `add_observations`, `search_nodes`, etc.) and the instructions to use them proactively.

<details open>
<summary><strong>Claude Code</strong></summary>

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

### Step 3: (Claude Code) Add hooks for automatic context injection

Hooks make the agent receive its memory briefing at the start of every session, and keep caches fresh after writes. **This step is optional but strongly recommended for Claude Code users.**

Add to `.claude/settings.json` (project or global):

```json
{
  "hooks": {
    "SessionStart": [
      { "command": "agent-recall-session-start" }
    ],
    "PostToolUse": [
      { "command": "agent-recall-post-tool-use" }
    ]
  }
}
```

| Hook | What it does |
|------|-------------|
| `SessionStart` | Injects AI briefing (or raw context) into the agent's system prompt when a session starts |
| `PostToolUse` | After the agent writes to memory, invalidates stale caches and regenerates vault files |

> **Other editors:** Hooks are Claude Code-specific. For other clients, use the [CLI](#cli) (`agent-recall generate`) or [Python API](#python-api) to generate and serve briefings.

### Step 4: Verify it works

Start a new session with your agent and look for:
- The agent should have memory tools listed (e.g., `create_entities`, `search_nodes`)
- If hooks are set up, the agent shows a "Memory is empty" message on first run
- Mention a person or make a decision — the agent should save it automatically
- Start another session — the agent should know about the person/decision

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
    children: [client-a, client-b]

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
| `cli` (default) | `backend: cli` | Claude Code installed | Free on Max/Team subscription | Creates a session file per call |
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
agent-recall set Alice person role Engineer  # Set a scoped slot
agent-recall get Alice role                # Get slot value
agent-recall entity Alice                  # Show entity details
agent-recall search "engineer"             # Search entities
agent-recall history Alice role            # Bitemporal slot history
agent-recall log Alice "Joined the team"   # Add observation
agent-recall generate my-agent --force     # Generate AI briefing
agent-recall refresh --force               # Refresh all briefings
agent-recall rename-scope old-name new-name  # Migrate data between scopes
```

## Python API

```python
from agent_recall import MemoryStore, ScopedView

with MemoryStore() as store:
    alice = store.resolve_entity("Alice", "person")
    acme = store.resolve_entity("Acme Corp", "client")

    store.set_slot(alice, "role", "Engineer", scope="global")
    store.set_slot(alice, "role", "Lead Engineer", scope="acme")
    store.add_observation(alice, "Prefers async communication", scope="acme")
    store.add_relation(alice, acme, "works_at")

    # Scoped view — local overrides parent
    view = ScopedView(store, ["global", "acme"])
    entity = view.get_entity("Alice")
    print(entity["slots"]["role"])  # "Lead Engineer" (acme overrides global)
```

See [`examples/quickstart.py`](examples/quickstart.py) for a runnable version.

---

## Born in Production

agent-recall was extracted from a live system managing real client projects at a digital agency — 30+ agents, 15+ clients, hundreds of scoped facts.

Why specific features exist:
- **Scope isolation** — two agents wrote conflicting data to the same entity
- **Adaptive caching** — briefings went stale during busy hours
- **AI summaries** — agents couldn't make sense of raw data dumps with hundreds of entries
- **Proactive saving instructions** — agents ignored memory tools until explicitly told to use them
- **Bitemporal slots** — needed to track what was true *when*, not just what's true now

## Development

```bash
git clone https://github.com/mnardit/agent-recall.git
cd agent-recall
pip install -e ".[dev]"
pytest
```

321 tests covering store, config, hierarchy, context assembly, AI briefings, vault generation, hooks, dedup, and MCP bridge.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT
