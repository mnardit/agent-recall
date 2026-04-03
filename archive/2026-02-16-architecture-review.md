# Architecture Review: agent-memory v0.1.0

3187 lines source, 2933 lines tests, 259 tests passing.

## Module Structure
- Clean DAG, no circular dependencies
- schema.py completely isolated (dead code)
- context_gen.py is God Module (809 lines, 5-6 responsibilities)
- hooks.py uses fcntl (Linux-only)

## Data Model
- 6 SQLite tables: entities, slots, observations, relations, log_entries, documents
- Bitemporal slots: correct approach for audit + scope override
- WAL mode + partial indexes: well-designed
- No schema migrations (blocker for v0.2+)
- LIKE search without FTS5 (OK for <1000 entities)
- Potential race in set_slot between UPDATE and INSERT

## API Surface
- 12 exports in __all__ — too many for v0.1
- LLMCaller type alias not exported (needed by users)
- Private functions imported in tests (fragile)

## Error Handling
- Good: empty name validation, config errors, timeout handling, _safe_filename
- Bad: dedup.py accesses store._conn directly, hooks swallow errors silently, MCPBridge skips missing entities without warning, no custom exception classes

## Extensibility
- New entity types: easy (just strings)
- New storage backends: very hard (no Protocol/ABC)
- New LLM providers: well-designed (pluggable callable)

## Performance
- read_graph() loads ALL entities/relations (no pagination)
- Orchestrator context: O(E*R) iteration
- search(): 3*patterns SQL queries (could UNION)
- dedup find_candidates(): O(n^2)

## Test Architecture
- Good coverage (259 tests), tmp_path fixtures, fake LLM callers
- Blind spots: mcp_server.py (0 tests), concurrent access, integration test

## Top 5 Actions
1. Split context_gen.py into 4 modules (templates, cache, generation, context assembly)
2. Add StorageProtocol + transaction() context manager
3. Schema migration system (schema_version table)
4. Platform-agnostic file locking (remove fcntl)
5. Tests for mcp_server.py + integration test
