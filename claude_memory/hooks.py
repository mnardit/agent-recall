"""Claude Code hooks — SessionStart and PostToolUse for agent memory.

These are meant to be installed as hook scripts. The CLI `init` or `install-hooks`
command writes them to the appropriate location.
"""
import fcntl
import json
import sys
import time
from pathlib import Path

from claude_memory.config import load_config
from claude_memory.context import assemble_context
from claude_memory.context_gen import read_cache
from claude_memory.store import MemoryStore
from claude_memory.vault_gen import generate_vault


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
    """PostToolUse hook — regenerates vault after MCP memory writes.

    Input: JSON from stdin with tool_name key.
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool = data.get("tool_name", "")
    if tool not in WRITE_TOOLS:
        return

    config = load_config()
    if not config.vault_dir or not config.vault_dir.exists():
        return

    # Rate limit
    rate_file = Path("/tmp/claude-memory-vault-regen-last")
    rate_seconds = 300
    if rate_file.exists():
        last = rate_file.stat().st_mtime
        if time.time() - last < rate_seconds:
            return

    # Acquire exclusive lock (non-blocking)
    lock_file = Path("/tmp/claude-memory-vault-regen.lock")
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
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


# --- Entry points ---

def main_session_start():
    """Entry point for SessionStart hook script."""
    session_start_hook()


def main_post_tool_use():
    """Entry point for PostToolUse hook script."""
    post_tool_use_hook()
