# Architecture Notes

## Config Resolution Order

Config values are resolved from multiple sources. Higher priority wins:

### db_path / cache_dir
1. Explicit YAML config (`db_path` or `database` alias)
2. Environment variable (`AGENT_RECALL_DB_PATH` / `AGENT_RECALL_CACHE_DIR`)
3. Package default (`~/.agent-recall/frames.db`)

### Agent scope chain (get_agent)
1. Explicit `scope_chain` in `agents.<slug>` config section
2. Orchestrator type → `["global"]` (tier 3)
3. Explicit tier assignment + hierarchy child lookup → `["global", parent, slug]`
4. System agent type → `["global", slug]` (tier 1)
5. Hierarchy child → `["global", parent, slug]` (tier 2)
6. Hierarchy parent → `["global", slug]` (tier 2)
7. Unknown slug → `["global", slug]` (tier 2, warning logged)

### Agent type (for prompt template selection)
1. Explicit `type` in `agents.<slug>` config
2. `agent_type` from scope chain resolution (orchestrator/system)
3. `agent_types` mapping in config
4. Hierarchy inference: child → "client", parent → "agency"
5. Default: `briefing.default_agent_type` or "personal"

### Per-agent briefing settings
Global `briefing` section merged with per-agent overrides in `agents.<slug>`:
`model`, `timeout`, `output_budget`, `raw_budget`, `cache_max_age`, `min_cache_age`, `adaptive`

## Scope Enforcement Boundary

Scope enforcement happens at **three layers**:

```
┌─────────────────────────────────────────────┐
│  MCP Server (mcp_server.py)                 │
│  - Determines slug from AGENT_RECALL_SLUG   │
│  - Creates MCPBridge with scope_chain       │
│  - No direct enforcement                    │
└───────────────┬─────────────────────────────┘
                │
┌───────────────▼─────────────────────────────┐
│  MCP Bridge (mcp_bridge.py)                 │  ◄── ENFORCEMENT LAYER
│  WRITES:                                    │
│  - _entity_writable() checks entity scopes  │
│  - Only enforced when chain > 1             │
│  - New/global entities: anyone can write    │
│  - create_relations: source must be writable│
│  - Input size limits enforced here          │
│  READS:                                     │
│  - _read_scope_set() for filtering          │
│  - _entity_visible() for entity access      │
│  - scope_reads=True filters by scope chain  │
│  - scope_reads=False (orchestrators): no    │
│    read filtering                           │
└───────────────┬─────────────────────────────┘
                │
┌───────────────▼─────────────────────────────┐
│  Store (store.py)                           │
│  - NO scope enforcement                     │
│  - Trusts the caller                        │
│  - All scope params are pass-through        │
│  - Direct store access bypasses all scopes  │
└─────────────────────────────────────────────┘
```

### Key decisions:
- **Store is scope-unaware** — keeps it simple, testable, reusable
- **Bridge enforces** — single enforcement point for MCP callers
- **Orchestrators bypass read filtering** via `scope_reads=False`
- **Tier 0 agents skip all enforcement** (chain is empty)
- **New entities are globally writable** — bootstrap problem: the first agent to mention an entity creates it
