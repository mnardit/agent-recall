"""Agent memory hooks — SessionStart and PostToolUse.

These are meant to be installed as hook scripts. The CLI `init` or `install-hooks`
command writes them to the appropriate location. Currently uses the Claude Code
hook protocol (JSON to stdout); other clients can integrate via the Python API.
"""
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]  # Windows — file locking unavailable
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from agent_recall.config import load_config
from agent_recall.context import assemble_context
from agent_recall.context_gen import (
    read_cache, get_cache_path, generate_briefing,
    clear_stale_marker, invalidate_cache, scope_to_agents,
)
from agent_recall.store import MemoryStore


# --- SessionStart Hook ---

def session_start_hook() -> None:
    """SessionStart hook — serves AI briefing from cache, falls back to raw context.

    Output: JSON to stdout with additionalContext key (Claude Code hook protocol).
    """
    try:
        slug = os.environ.get("AGENT_RECALL_SLUG") or Path.cwd().name

        config = load_config()
        agent = config.get_agent(slug)

        if agent.tier == 0 or not agent.chain:
            return

        if not config.db_path.exists():
            return

        # Check for stale cache — regenerate if adaptive mode enabled
        from agent_recall.context_gen.cache import _sanitize_slug
        stale_path = config.cache_dir / f"{_sanitize_slug(slug)}.stale"
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
            return

        # Cold start — memory exists but is empty
        print(json.dumps({"additionalContext": (
            "## Memory\n\n"
            "Memory is empty. As you work, save important information using the "
            "memory MCP tools — people, decisions, project context. "
            "Future sessions will start with this knowledge automatically."
        )}))
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

    _tmpdir = Path(tempfile.gettempdir())
    rate_file = _tmpdir / "agent-recall-vault-regen-last"
    rate_seconds = 300

    # Acquire exclusive lock (non-blocking) — skip locking on Windows
    lock_file = _tmpdir / "agent-recall-vault-regen.lock"
    lock_fd = None
    try:
        lock_fd = lock_file.open("w")
    except OSError:
        return
    if fcntl is not None:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fd.close()
            return

    try:
        # Rate limit check inside lock to prevent TOCTOU race
        if rate_file.exists():
            last = rate_file.stat().st_mtime
            if time.time() - last < rate_seconds:
                return

        from agent_recall.contrib.vault_gen import generate_vault
        store = MemoryStore(config.db_path)
        try:
            generate_vault(store, config.vault_dir)
        finally:
            store.close()
        rate_file.write_text(str(time.time()))
    finally:
        if fcntl is not None and lock_fd is not None:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        if lock_fd is not None:
            lock_fd.close()

    # Auto-commit if enabled
    if config.vault_auto_commit:
        from agent_recall.contrib.vault_gen import _git_auto_commit
        _git_auto_commit(config.vault_dir)


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

    # Get the calling agent's scope chain for validation
    slug = os.environ.get("AGENT_RECALL_SLUG") or ""
    try:
        if not slug:
            slug = Path.cwd().name
    except Exception:
        pass
    # Only validate scopes for explicitly configured agents
    known_agents = set(config.all_agents())
    agent = config.get_agent(slug) if slug else None
    enforce_scope = slug in known_agents
    allowed_scopes: set[str] = set()
    if agent and enforce_scope:
        allowed_scopes = set(agent.chain)
        # Include children from hierarchy — parents can invalidate children
        for scope in list(allowed_scopes):
            allowed_scopes |= config.scope_children(scope)

    scopes: set[str] = set()

    # Current agent's scope (from slug, not CWD)
    if slug:
        scopes.add(slug)

    # Extract scopes from tool_input — but only accept scopes in agent's chain.
    # Note: MCP tools send observations as plain strings, not dicts with scope fields.
    # Scope is set at the MCPBridge level (default_scope), not per-observation.
    # We only check the top-level "scope" field which some tools do pass directly.
    if isinstance(tool_input, dict):
        if "scope" in tool_input:
            candidate = tool_input["scope"]
            if not allowed_scopes or candidate in allowed_scopes:
                scopes.add(candidate)

    # Map scopes to affected agents and invalidate
    affected: set[str] = set()
    for scope in scopes:
        affected.update(scope_to_agents(scope, config))

    if affected:
        invalidate_cache(sorted(affected), config.cache_dir)
