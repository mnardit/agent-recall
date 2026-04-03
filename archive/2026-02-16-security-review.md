# Security Review: agent-memory v0.1.0

**0 CRITICAL, 2 HIGH, 5 MEDIUM, 2 LOW**

## HIGH
1. **vault_gen.py:69-76 + hooks.py:152-159** — `bash -c` command injection via vault path. Fix: use `git -C` instead of `bash -c` with string concat.

## MEDIUM
1. **vault_gen.py:20-25** — _safe_filename() incomplete (`.././..` passes). Add regex, length limit, resolve() check.
2. **config.py:173** — No YAML file size limit (billion laughs attack). Add MAX_CONFIG_SIZE check.
3. **mcp_server.py:32-38** — Singleton race condition (no thread lock). Add threading.Lock.
4. **context_gen.py:196** — Env var leakage in LLM subprocess (only strips CLAUDECODE). Consider whitelist.
5. **store.py:540** — SQL LIKE with custom escape (works but fragile). FTS5 would be safer.

## LOW
1. **pyproject.toml** — pyyaml>=6.0 should be >=6.0.2 (CVE-2020-14343)
2. **dedup.py:136** — Direct _conn access bypasses store encapsulation

## Positive Findings
- All SQL uses parameterized queries (no injection)
- Scope enforcement in MCPBridge works correctly
- No eval/exec anywhere
- No hardcoded secrets
- Bitemporal archiving preserves audit trail
