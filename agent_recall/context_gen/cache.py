"""Cache management for AI briefings — read, freshness check, invalidation."""
import json as _json
import logging
import re as _re
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_recall.config import MemoryConfig, load_config

log = logging.getLogger(__name__)


def _sanitize_slug(slug: str) -> str:
    """Sanitize slug for safe use in file paths."""
    safe = _re.sub(r'[^a-zA-Z0-9_-]', '_', slug)
    return safe or 'unknown'

# Defaults (overridable via config.briefing)
DEFAULT_CACHE_MAX_AGE = 86400  # 24 hours

MAX_LOG_ENTRIES = 10


def _default_cache_dir() -> Path:
    return Path.home() / ".agent-recall" / "context_cache"


def is_cache_fresh(slug: str, cache_dir: Path | None = None,
                   max_age: int = DEFAULT_CACHE_MAX_AGE) -> bool:
    """Check if cached briefing exists, is younger than max_age, and not stale."""
    cache_dir = cache_dir or _default_cache_dir()
    safe = _sanitize_slug(slug)
    cache_path = cache_dir / f"{safe}.md"
    if not cache_path.exists():
        return False
    # Check for .stale marker (set by adaptive invalidation)
    stale_path = cache_dir / f"{safe}.stale"
    if stale_path.exists():
        return False
    age = time.time() - cache_path.stat().st_mtime
    return age < max_age


def get_cache_path(slug: str, cache_dir: Path | None = None) -> Path:
    """Return cache file path for an agent."""
    cache_dir = cache_dir or _default_cache_dir()
    return cache_dir / f"{_sanitize_slug(slug)}.md"


def read_cache(slug: str, cache_dir: Path | None = None,
               max_age: int = DEFAULT_CACHE_MAX_AGE) -> str | None:
    """Read cached briefing if fresh, else None."""
    if not is_cache_fresh(slug, cache_dir, max_age):
        return None
    try:
        return get_cache_path(slug, cache_dir).read_text(encoding="utf-8")
    except OSError:
        return None


# --- Cache Invalidation (adaptive mode) ---

def invalidate_cache(slugs: list[str], cache_dir: Path | None = None) -> list[str]:
    """Mark agent caches as stale by creating .stale marker files.

    Returns list of slugs that were invalidated.
    """
    cache_dir = cache_dir or _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    invalidated = []
    for slug in slugs:
        safe = _sanitize_slug(slug)
        cache_path = cache_dir / f"{safe}.md"
        if cache_path.exists():
            stale_path = cache_dir / f"{safe}.stale"
            stale_path.write_text(str(time.time()))
            invalidated.append(slug)
            log.info("Invalidated cache for %s", slug)
    return invalidated


def clear_stale_marker(slug: str, cache_dir: Path | None = None) -> None:
    """Remove .stale marker after successful regeneration."""
    cache_dir = cache_dir or _default_cache_dir()
    stale_path = cache_dir / f"{_sanitize_slug(slug)}.stale"
    if stale_path.exists():
        stale_path.unlink()


def scope_to_agents(scope: str, config: MemoryConfig) -> list[str]:
    """Map a write scope to affected agent slugs.

    A write to scope X invalidates:
    - Agent X itself (if it's a known agent)
    - Parent agent (if X is a hierarchy child)
    - Orchestrator agents (they see everything)
    """
    affected: set[str] = set()
    all_known = set(config.all_agents())

    # The scope itself
    if scope in all_known:
        affected.add(scope)

    # Parent: if scope is a hierarchy child, parent is affected
    for parent, children in config.hierarchy.items():
        if scope in children:
            affected.add(parent)

    # Orchestrator agents always affected
    for slug in config.agent_types.get("orchestrator", []):
        affected.add(slug)

    return sorted(affected)


def get_agent_status(slug: str, config: MemoryConfig | None = None) -> dict:
    """Get cache/briefing status for an agent.

    Args:
        slug: Agent identifier. Must be non-empty.
        config: Memory configuration. Loaded from default paths if None.

    Returns:
        Dict with keys: slug, has_cache, is_fresh, is_stale, enabled, model,
        template_type, size_bytes, generated_at, generated_at_iso, age_seconds.

    Raises:
        ValueError: If slug is empty.
    """
    from agent_recall.context_gen.llm import DEFAULT_MODEL

    if not slug or not slug.strip():
        raise ValueError("Agent slug cannot be empty")

    config = config or load_config()
    cache_dir = config.cache_dir
    cache_path = get_cache_path(slug, cache_dir)
    agent_briefing = config.get_agent_briefing(slug)
    stale_path = cache_dir / f"{_sanitize_slug(slug)}.stale"

    has_cache = cache_path.exists()
    if has_cache:
        st = cache_path.stat()
        generated_at = st.st_mtime
        size = st.st_size
    else:
        generated_at = None
        size = 0
    age = time.time() - generated_at if generated_at is not None else None
    generated_iso = (datetime.fromtimestamp(generated_at, tz=timezone.utc).isoformat()
                     if generated_at is not None else None)

    return {
        "slug": slug,
        "has_cache": has_cache,
        "is_stale": stale_path.exists(),
        "is_fresh": is_cache_fresh(slug, cache_dir,
                                    agent_briefing.get("cache_max_age", DEFAULT_CACHE_MAX_AGE)),
        "enabled": config.get_agent_enabled(slug),
        "model": agent_briefing.get("model", DEFAULT_MODEL),
        "template_type": config.get_agent_template(slug) or config.get_agent_type(slug),
        "size_bytes": size,
        "generated_at": generated_at,
        "generated_at_iso": generated_iso,
        "age_seconds": round(age) if age is not None else None,
    }


def get_all_statuses(config: MemoryConfig | None = None) -> dict[str, dict]:
    """Get cache status for all known agents in one call.

    Args:
        config: Memory configuration. Loaded from default paths if None.

    Returns:
        Dict mapping agent slug to status dict (same format as ``get_agent_status``).
    """
    config = config or load_config()
    return {slug: get_agent_status(slug, config) for slug in config.all_agents()}


def _save_generation_log(slug: str, entry: dict, cache_dir: Path,
                         max_entries: int = MAX_LOG_ENTRIES) -> None:
    """Append a log entry to <cache_dir>/<slug>.log.json, rotating old entries."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_path = cache_dir / f"{_sanitize_slug(slug)}.log.json"
    entries: list[dict] = []
    if log_path.exists():
        try:
            entries = _json.loads(log_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    entries = entries[-max_entries:]
    log_path.write_text(_json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def get_generation_logs(slug: str, config: MemoryConfig | None = None) -> list[dict]:
    """Read generation log entries for an agent.

    Logs are created by ``generate_briefing`` and stored in ``<cache_dir>/<slug>.log.json``.
    Only the last 10 entries are kept (rotation).

    Args:
        slug: Agent identifier.
        config: Memory configuration. Loaded from default paths if None.

    Returns:
        List of log entry dicts with keys: slug, timestamp, model, agent_type,
        duration_ms, input_chars, output_chars, input_tokens, output_tokens, status.
        Empty list if no logs exist.
    """
    config = config or load_config()
    log_path = config.cache_dir / f"{_sanitize_slug(slug)}.log.json"
    if not log_path.exists():
        return []
    try:
        return _json.loads(log_path.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError):
        return []
