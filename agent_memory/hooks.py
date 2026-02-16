"""Claude Code hooks — SessionStart and PostToolUse for agent memory.

These are meant to be installed as hook scripts. The CLI `init` or `install-hooks`
command writes them to the appropriate location.
"""
import fcntl
import json
import sys
import tempfile
import time
from pathlib import Path

from agent_memory.config import load_config
from agent_memory.context import assemble_context
from agent_memory.context_gen import (
    read_cache, get_cache_path, generate_briefing,
    clear_stale_marker, invalidate_cache, scope_to_agents,
)
from agent_memory.store import MemoryStore
from agent_memory.vault_gen import generate_vault


# --- SessionStart Hook ---

def session_start_hook() -> None:
    """SessionStart hook — serves AI briefing from cache, falls back to raw context.

    Output: JSON to stdout with additionalContext key (Claude Code hook protocol).
    """
    try:
        cwd = Path.cwd()
        slug = cwd.name

        config = load_config()
        agent = config.get_agent(slug)

        if agent.tier == 0 or not agent.chain:
            return

        if not config.db_path.exists():
            return

        # Check for stale cache — regenerate if adaptive mode enabled
        stale_path = config.cache_dir / f"{slug}.stale"
        if stale_path.exists():
            min_age = config.briefing.get("min_cache_age", 1800)
            cache_path = get_cache_path(slug, config.cache_dir)
            can_regen = (not cache_path.exists() or
                         time.time() - cache_path.stat().st_mtime >= min_age)
            if can_regen and config.briefing.get("adaptive", False):
                try:
                    generate_briefing(slug, config=config, force=True)
                except Exception as e:
                    print(f"Stale regen failed: {e}", file=sys.stderr)
            else:
                # Clear stale marker anyway — serve existing cache
                clear_stale_marker(slug, config.cache_dir)

        # Try cached AI briefing first
        cached = read_cache(slug, cache_dir=config.cache_dir)
        if cached:
            print(json.dumps({"additionalContext": f"## Agent Briefing\n\n{cached}"}))
            return

        # Fallback: raw context assembly
        store = MemoryStore(config.db_path)
        try:
            ctx = assemble_context(
                store, chain=agent.chain, tier=agent.tier,
                vault_projects_dir=(config.vault_dir / "projects"
                                    if config.vault_dir else None),
                task_header=config.vault_task_header,
            )
        finally:
            store.close()

        if ctx:
            print(json.dumps({"additionalContext": f"## Memory Context\n\n{ctx}"}))
    except Exception as e:
        print(f"Memory context error: {e}", file=sys.stderr)


# --- PostToolUse Hook ---

WRITE_TOOLS = {
    "mcp__memory__create_entities",
    "mcp__memory__create_relations",
    "mcp__memory__add_observations",
    "mcp__memory__delete_entities",
    "mcp__memory__delete_relations",
    "mcp__memory__delete_observations",
}


def post_tool_use_hook() -> None:
    """PostToolUse hook — regenerates vault and invalidates caches after MCP memory writes.

    Input: JSON from stdin with tool_name and tool_input keys.
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool = data.get("tool_name", "")
    if tool not in WRITE_TOOLS:
        return

    config = load_config()

    # Adaptive cache invalidation — determine affected scopes from tool input
    if config.briefing.get("adaptive", False):
        _invalidate_affected_agents(data, config)

    if not config.vault_dir or not config.vault_dir.exists():
        return

    # Rate limit
    _tmpdir = Path(tempfile.gettempdir())
    rate_file = _tmpdir / "agent-memory-vault-regen-last"
    rate_seconds = 300
    if rate_file.exists():
        last = rate_file.stat().st_mtime
        if time.time() - last < rate_seconds:
            return

    # Acquire exclusive lock (non-blocking)
    lock_file = _tmpdir / "agent-memory-vault-regen.lock"
    lock_fd = lock_file.open("w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        return

    try:
        rate_file.write_text(str(time.time()))
        store = MemoryStore(config.db_path)
        try:
            generate_vault(store, config.vault_dir)
        finally:
            store.close()
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()

    # Auto-commit if enabled
    if config.vault_auto_commit:
        import shlex
        import subprocess
        vault_dir = config.vault_dir
        subprocess.Popen(
            ["bash", "-c",
             f"cd {shlex.quote(str(vault_dir))} && "
             "git add people/ clients/ decisions/ && "
             "git diff --cached --quiet || "
             "git commit -m 'auto: regenerate from frames.db' && git push"],
            stdout=subprocess.DEVNULL,
        )


# --- Entry points ---

def main_session_start():
    """Entry point for SessionStart hook script."""
    session_start_hook()


def main_post_tool_use():
    """Entry point for PostToolUse hook script."""
    post_tool_use_hook()


def _invalidate_affected_agents(data: dict, config: "MemoryConfig") -> None:
    """Determine affected scopes from MCP tool input and invalidate their caches."""
    tool_input = data.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {}

    scopes: set[str] = set()

    # Current agent's scope (CWD name)
    try:
        scopes.add(Path.cwd().name)
    except Exception:
        pass

    # Extract scopes from tool_input — MCP tools pass scope in various fields
    if isinstance(tool_input, dict):
        # Direct scope field (add_observations, set_slot)
        if "scope" in tool_input:
            scopes.add(tool_input["scope"])
        # Entities list (create_entities) — each may have observations with scope
        for entity in tool_input.get("entities", []):
            if isinstance(entity, dict):
                for obs in entity.get("observations", []):
                    if isinstance(obs, dict) and "scope" in obs:
                        scopes.add(obs["scope"])
        # Observations list (add_observations)
        for obs in tool_input.get("observations", []):
            if isinstance(obs, dict) and "scope" in obs:
                scopes.add(obs["scope"])

    # Map scopes to affected agents and invalidate
    affected: set[str] = set()
    for scope in scopes:
        affected.update(scope_to_agents(scope, config))

    if affected:
        invalidate_cache(sorted(affected), config.cache_dir)
