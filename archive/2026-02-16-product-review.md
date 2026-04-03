# Product Review: agent-memory v0.1.0

## Value Proposition
- Persistent memory for multi-agent systems with scope hierarchy + AI briefings
- Target: developers managing 5+ AI agents with overlapping data
- Positioning too narrow ("Claude Code") — should say "MCP-compatible AI agents"
- AI briefing generation is key differentiator, underemphasized in README

## Competitive Landscape
| Product | Stars | Scope/Multi-agent | AI Briefings | MCP |
|---------|-------|-------------------|--------------|-----|
| Mem0 | 41k+ | No (user-level) | No | Wrapper |
| LangMem | ~2k | No (per-graph) | No | No |
| Letta/MemGPT | 12k+ | No | No | No |
| @mcp/server-memory | ~5k | No | No | Native |
| agent-memory | 0 | **Yes** (scope chains) | **Yes** (6 templates) | Native |

## Feature Completeness
- CRUD, scope hierarchy, MCP server, CLI, config, tests: Done
- Missing: schema migrations, FTS5 search, quickstart demo, init --full
- Nice-to-have: export/import, vector search, async, dashboard

## Adoption Barriers
1. Claude CLI dependency for AI briefings
2. 4 manual steps to get started (too much friction)
3. No CI badge = red flag for adopters
4. fcntl breaks Windows
5. schema.py is dead code

## Top 5 Actions
1. One-command quickstart (`init --full` + `demo`)
2. Pluggable LLM via Anthropic SDK (`agent-memory[anthropic]`)
3. GitHub CI + PyPI publish
4. Windows compatibility (fcntl → filelock)
5. Quickstart guide + architecture diagram
