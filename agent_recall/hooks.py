"""Agent memory hooks — SessionStart and PostToolUse.

These are meant to be installed as hook scripts. The CLI `init` or `install-hooks`
command writes them to the appropriate location. Currently uses the Claude Code
hook protocol (JSON to stdout); other clients can integrate via the Python API.

v0.5.0: SessionStart injects hot cache; PostToolUse triggers auto_capture.
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

def _format_hot_cache(hot_items: list[dict]) -> str:
    """Format hot cache items for context injection."""
    if not hot_items:
        return ""
    lines = ["## 🧠 Hot Memory (auto-injected)", ""]
    for item in hot_items:
        entity = item.get("entity_name", "unknown")
        etype = item.get("entity_type", "")
        text = item.get("text", "")
        salience = item.get("salience_score", 0)
        lines.append(
            f"- **[{etype}] {entity}** (salience={salience:.2f}): {text[:200]}"
        )
    return "\n".join(lines) + "\n"


def _inject_hot_cache(
    config: "MemoryConfig", scope: str, base_context: str,
) -> str:
    """Inject hot cache items into the context string."""
    try:
        from agent_recall.knowledge_tiers import KnowledgeTierManager
        store = MemoryStore(config.db_path)
        try:
            mgr = KnowledgeTierManager(store)
            hot_items = mgr.get_hot_cache(scope)
            if hot_items:
                hot_block = _format_hot_cache(hot_items)
                return hot_block + "\n" + base_context
        finally:
            store.close()
    except Exception:
        pass
    return base_context


def _safe_print_context(ctx: str, max_bytes: int = 3500) -> None:
    """Print additionalContext JSON safe from Windows pipe deadlock (v0.5.1)."""
    encoded = ctx.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
        notice = "\n... (truncated to avoid pipe buffer deadlock, full context in OMC)"
        ctx = truncated[:max_bytes - len(notice.encode())] + notice
    print(json.dumps({"additionalContext": ctx}))


def session_start_hook() -> None:
    """SessionStart hook — serves AI briefing from cache, falls back to raw context.

    v0.5.0: Hot cache items are prepended to the context for instant recall.
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

        scope = agent.chain[-1] if agent.chain else "global"

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
            ctx = _inject_hot_cache(config, scope, f"## Agent Briefing\n\n{cached}")
            _safe_print_context(ctx)
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
            final_ctx = _inject_hot_cache(config, scope, f"## Memory Context\n\n{ctx}")
            _safe_print_context(final_ctx)
            return

        # Cold start — try hot cache even when memory appears empty
        hot_ctx = _inject_hot_cache(config, scope, "")
        if hot_ctx.strip():
            _safe_print_context(hot_ctx)
            return

        # Truly cold start — memory exists but is empty
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

RETRIEVAL_TOOLS = {
    "mcp__agent-recall__omc_search",
    "mcp__agent-recall__vector_search",
    "mcp__agent-recall__search_nodes",
    "mcp__agent-recall__timeline",
    "mcp__agent-recall__open_nodes",
    "mcp__agent-recall__read_graph",
}


def _run_auto_capture(
    config: "MemoryConfig", tool_name: str,
    tool_input: dict, tool_output: str, scope: str,
) -> None:
    """Run auto-capture on tool output (fire-and-forget, non-blocking)."""
    if not tool_output or not tool_output.strip():
        return
    if len(tool_output) < 20:
        return
    try:
        from agent_recall.auto_capture import (
            AutoCaptureEngine, enqueue_auto_capture,
        )
        store = MemoryStore(config.db_path)
        try:
            engine = AutoCaptureEngine(store)
            captured_count = engine.capture_from_tool_output(
                tool_name, tool_input, tool_output,
            )
            if captured_count > 0:
                logger = __import__("logging").getLogger("agent_recall.hooks")
                logger.debug(
                    "Auto-captured %d patterns from %s", captured_count, tool_name,
                )
        finally:
            store.close()
    except Exception:
        pass  # Auto-capture is best-effort; never break the hook


_DECISION_PATTERNS = [
    r"decided to\b", r"\bchose\b.*\bbecause\b", r"\brule:\b", r"\bmust\b",
    r"\bnever\b.*\bagain\b", r"\b禁止\b", r"\b必须\b", r"\b教训\b",
    r"\bbest practice\b", r"\bdecision:\b", r"\bconstraint:\b",
    r"\balways\b.*\bwhen\b", r"\blesson learned\b",
]
import re as _re2
_DECISION_RE = [_re2.compile(p, _re2.IGNORECASE) for p in _DECISION_PATTERNS]


def _has_decision_pattern(text: str) -> bool:
    """Check if text contains a decision/constraint/lesson pattern."""
    if len(text) < 30:
        return False
    for pat in _DECISION_RE:
        if pat.search(text):
            return True
    return False


# Skill auto-trigger patterns: (skill_name, [trigger_patterns], reason)
_SKILL_TRIGGERS = [
    ("systematic-debugging", [
        r"error:", r"traceback", r"exception:", r"fail", r"bug",
        r"not working", r"broken", r"crash", r"unexpected",
    ], "Error/failure detected in tool output"),
    ("test-driven-development", [
        r"implement", r"feature", r"bugfix", r"fix",
    ], "New feature or bugfix being implemented"),
    ("verification-before-completion", [
        r"done", r"complete", r"fixed", r"passing", r"verified",
        r"commit", r"merge",
    ], "Claiming completion — verify before committing"),
    ("brainstorming", [
        r"design", r"architecture", r"plan", r"approach",
        r"how should", r"what is the best",
    ], "Design/architecture decision needed"),
    ("code-review-and-quality", [
        r"write.*code", r"implement", r"refactor", r"rewrite",
    ], "Code written — review for quality"),
    ("using-git-worktrees", [
        r"multiple files", r"refactor", r"core file",
        r"CLAUDE.md", r"SKILL.md", r"settings.json",
    ], "Multi-file change or core file — isolate in worktree"),
    ("security-and-hardening", [
        r"password", r"secret", r"token", r"api key",
        r"auth", r"credential", r"sensitive",
    ], "Security-sensitive code — harden before commit"),
    ("dispatching-parallel-agents", [
        r"parallel", r"both", r"all three", r"independently",
        r"concurrently", r"simultaneously",
    ], "Multiple independent tasks — dispatch in parallel"),
]

import re as _re3
_SKILL_TRIGGER_RE = [
    (name, [_re3.compile(p, _re3.IGNORECASE) for p in patterns], reason)
    for name, patterns, reason in _SKILL_TRIGGERS
]


def _detect_skill_triggers(tool_name: str, text: str) -> list[dict]:
    """Detect skill trigger conditions in tool output and return recommendations."""
    if len(text) < 10:
        return []
    recommendations = []
    for skill_name, patterns, reason in _SKILL_TRIGGER_RE:
        for pat in patterns:
            if pat.search(text):
                recommendations.append({
                    "skill": skill_name,
                    "reason": reason,
                    "matched": pat.pattern,
                })
                break  # One match per skill is enough
    return recommendations[:3]  # Max 3 recommendations


def post_tool_use_hook() -> None:
    """PostToolUse hook — vault regen + cache invalidation + auto-capture.

    Input: JSON from stdin with tool_name, tool_input, and tool_output keys.
    """
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_output = data.get("tool_output", "")

    # Normalize tool_input to dict
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {}

    config = load_config()

    # Determine scope from environment
    slug = os.environ.get("AGENT_RECALL_SLUG") or ""
    try:
        if not slug:
            slug = Path.cwd().name
    except Exception:
        pass
    try:
        agent = config.get_agent(slug) if slug else None
        scope = agent.chain[-1] if agent and agent.chain else "global"
    except Exception:
        scope = "global"

    # v0.5.0: Auto-capture patterns from ALL tool outputs (not just memory writes)
    if config.auto_capture.enabled and tool_output:
        _run_auto_capture(config, tool, tool_input, tool_output, scope)

    # v0.6.0: Retrieval feedback — mark recent retrievals as used when any tool succeeds
    # Closes the feedback loop: search → retrieve → use → mark helpful → improve ranking
    # v0.8.0: Skill auto-trigger — detect patterns and recommend skills
    # Writes to OMC hot cache so next turn injects skill recommendation
    try:
        if tool_name and tool_input:
            recommended = _detect_skill_triggers(tool_name, str(tool_input)[:2000])
            if recommended:
                store3 = MemoryStore(config.db_path)
                try:
                    for rec in recommended:
                        # Create a skill_trigger entity observation
                        de = store3.find_entity("skill-triggers") or \
                             store3.create_entity("skill-triggers", "decision")
                        text = f"SKILL:{rec['skill']}|REASON:{rec['reason']}|FROM:{tool_name}"
                        store3.add_observation(de, text, scope="global")
                        # Promote to hot so SessionStart injects it
                        obs_rows = store3._conn.execute(
                            "SELECT id FROM observations WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
                            (de,)
                        ).fetchall()
                        if obs_rows:
                            store3.set_tier(obs_rows[0]["id"], "hot", source="auto_trigger")
                    store3._conn.commit()
                finally:
                    store3.close()
    except Exception:
        pass  # Best-effort

    # v0.7.0: Decision extraction — capture decisions from assistant responses
    # Detects patterns like "decided to", "chose X because", "rule: ", etc.
    try:
        if tool_name in ("Write", "Edit", "Skill") and tool_input:
            input_str = str(tool_input)
            if _has_decision_pattern(input_str):
                store2 = MemoryStore(config.db_path)
                try:
                    de = store2.find_entity("auto-captured-decisions") or \
                         store2.create_entity("auto-captured-decisions", "decision")
                    # Truncate to avoid storing huge files
                    snippet = input_str[:500]
                    store2.add_observation(de, snippet, scope="global")
                    store2._conn.commit()
                finally:
                    store2.close()
    except Exception:
        pass  # Best-effort decision capture
    # ponytail: B4 fix — only mark as used for actual retrieval tools
    RETRIEVAL_TOOLS = {
        "mcp__agent-recall__omc_search",
        "mcp__agent-recall__vector_search",
        "mcp__agent-recall__search_nodes",
        "mcp__agent-recall__open_nodes",
        "mcp__agent-recall__timeline",
        "mcp__agent-recall__multi_graph_search",
        "mcp__agent-recall__omc_multi_graph_search",
        "mcp__agent-recall__omc_wake_up",
    }
    try:
        from agent_recall.retrieval_feedback import RetrievalFeedback
        store = MemoryStore(config.db_path)
        try:
            fb = RetrievalFeedback(store)
            if tool in RETRIEVAL_TOOLS:
                affected = store._conn.execute('''
                    SELECT DISTINCT observation_id FROM retrieval_events
                    WHERE was_used = 0
                      AND created_at > datetime('now', '-2 minutes')
                ''').fetchall()
                updated = store._conn.execute('''
                    UPDATE retrieval_events
                    SET was_used = 1, feedback = ?
                    WHERE was_used = 0
                      AND created_at > datetime('now', '-2 minutes')
                ''', (f'auto: {tool}',)).rowcount
                if updated > 0:
                    store._conn.commit()
                for row in affected:
                    if row["observation_id"]:
                        try:
                            store.update_access(row["observation_id"])
                        except Exception:
                            pass
            else:
                # Non-retrieval tool: explicitly mark as not used
                store._conn.execute('''
                    UPDATE retrieval_events
                    SET was_used = -1, feedback = ?
                    WHERE was_used = 0
                      AND created_at > datetime('now', '-2 minutes')
                ''', (f'auto: {tool} (not retrieval)',))
                store._conn.commit()
        finally:
            store.close()
    except Exception:
        pass  # Best-effort feedback; never break the hook

    if tool not in WRITE_TOOLS:
        return

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
