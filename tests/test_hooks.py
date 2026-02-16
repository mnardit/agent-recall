"""Tests for Claude Code hooks — SessionStart and PostToolUse."""
import json
import pytest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_recall.config import MemoryConfig
from agent_recall.store import MemoryStore
from agent_recall.hooks import WRITE_TOOLS, _invalidate_affected_agents


@pytest.fixture
def config(tmp_path):
    return MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        hierarchy={"acme": ["proj-a"]},
    )


@pytest.fixture
def seeded_config(config):
    """Config with some data in the DB."""
    store = MemoryStore(config.db_path)
    for name, role in [("Alice", "Engineer"), ("Bob", "Designer"),
                       ("Carol", "Manager")]:
        eid = store.resolve_entity(name, "person")
        store.set_slot(eid, "role", role, scope="acme")
        store.set_slot(eid, "email", f"{name.lower()}@example.com", scope="acme")
        store.add_observation(eid, f"{name} works on the project", scope="acme")
    store.close()
    return config


# --- SessionStart hook ---

def test_session_start_with_cache(seeded_config, capsys):
    """Hook returns cached briefing if available."""
    config = seeded_config
    cache_dir = config.cache_dir
    cache_dir.mkdir(parents=True)
    (cache_dir / "acme.md").write_text("# Cached Briefing\nHello!")

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath:
        # Mock cwd to return "acme" as slug
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_cwd.parent.name = "projects"

        from agent_recall.hooks import session_start_hook
        session_start_hook()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Cached Briefing" in data["additionalContext"]


def test_session_start_raw_fallback(seeded_config, capsys):
    """Hook falls back to raw context when no cache."""
    config = seeded_config

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_cwd.parent.name = "projects"
        # read_cache needs real Path for cache_dir
        with patch("agent_recall.hooks.read_cache", return_value=None):
            from agent_recall.hooks import session_start_hook
            session_start_hook()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Memory Context" in data["additionalContext"]
    assert "Alice" in data["additionalContext"]


def test_session_start_tier0_silent(seeded_config, capsys):
    """Tier 0 agents produce no output."""
    config = seeded_config
    config.tiers = {0: ["infra-bot"]}

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "infra-bot"
        mock_cwd.parent.name = "projects"

        from agent_recall.hooks import session_start_hook
        session_start_hook()

    captured = capsys.readouterr()
    assert captured.out == ""


def test_session_start_cold_start(config, capsys):
    """Empty database shows cold-start message nudging agent to save facts."""
    # Create empty DB (no entities)
    store = MemoryStore(config.db_path)
    store.close()

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_cwd.parent.name = "projects"
        with patch("agent_recall.hooks.read_cache", return_value=None):
            from agent_recall.hooks import session_start_hook
            session_start_hook()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Memory is empty" in data["additionalContext"]
    assert "memory MCP tools" in data["additionalContext"]


# --- PostToolUse hook ---

def test_post_tool_use_write_tools():
    """All expected write tools are in the set."""
    assert "mcp__memory__create_entities" in WRITE_TOOLS
    assert "mcp__memory__add_observations" in WRITE_TOOLS
    assert len(WRITE_TOOLS) == 6


def test_post_tool_use_ignores_read_tools(seeded_config):
    """Non-write tools are ignored."""
    config = seeded_config
    config.vault_dir = Path("/tmp/test-vault")

    stdin_data = json.dumps({"tool_name": "mcp__memory__read_graph"})
    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("sys.stdin", StringIO(stdin_data)), \
         patch("agent_recall.hooks.generate_vault") as mock_gen:
        from agent_recall.hooks import post_tool_use_hook
        post_tool_use_hook()
    mock_gen.assert_not_called()


# --- Adaptive cache invalidation ---

@pytest.fixture
def adaptive_config(tmp_path):
    cfg = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        hierarchy={"acme": ["proj-a", "proj-b"]},
        agent_types={"orchestrator": ["boss"]},
        briefing={"adaptive": True, "min_cache_age": 0},
    )
    cfg.cache_dir.mkdir(parents=True)
    return cfg


def _seed_cache(cache_dir, slug):
    (cache_dir / f"{slug}.md").write_text(f"## Briefing for {slug}")


def test_invalidate_from_cwd_scope(adaptive_config, monkeypatch, tmp_path):
    """CWD name is used as scope — invalidates self + parent + orchestrator."""
    cwd = tmp_path / "proj-a"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _seed_cache(adaptive_config.cache_dir, "proj-a")
    _seed_cache(adaptive_config.cache_dir, "acme")
    _seed_cache(adaptive_config.cache_dir, "boss")

    data = {"tool_name": "mcp__memory__add_observations", "tool_input": {}}
    _invalidate_affected_agents(data, adaptive_config)

    assert (adaptive_config.cache_dir / "proj-a.stale").exists()
    assert (adaptive_config.cache_dir / "acme.stale").exists()
    assert (adaptive_config.cache_dir / "boss.stale").exists()


def test_invalidate_from_tool_input_scope(adaptive_config, monkeypatch, tmp_path):
    """Scope in tool_input is extracted and used for cross-agent invalidation."""
    cwd = tmp_path / "some-agent"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _seed_cache(adaptive_config.cache_dir, "proj-b")
    _seed_cache(adaptive_config.cache_dir, "acme")
    _seed_cache(adaptive_config.cache_dir, "boss")

    data = {
        "tool_name": "mcp__memory__add_observations",
        "tool_input": {"scope": "proj-b"},
    }
    _invalidate_affected_agents(data, adaptive_config)

    assert (adaptive_config.cache_dir / "proj-b.stale").exists()
    assert (adaptive_config.cache_dir / "acme.stale").exists()


def test_invalidate_from_create_entities_observations(adaptive_config, monkeypatch, tmp_path):
    """Scopes extracted from create_entities observations field."""
    cwd = tmp_path / "acme"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    _seed_cache(adaptive_config.cache_dir, "proj-a")
    _seed_cache(adaptive_config.cache_dir, "boss")

    data = {
        "tool_name": "mcp__memory__create_entities",
        "tool_input": {
            "entities": [
                {"name": "Alice", "entityType": "person",
                 "observations": [{"text": "Dev", "scope": "proj-a"}]},
            ]
        },
    }
    _invalidate_affected_agents(data, adaptive_config)

    assert (adaptive_config.cache_dir / "proj-a.stale").exists()


def test_session_start_stale_regen_adaptive(seeded_config, capsys):
    """Stale marker + adaptive=True → regenerates on SessionStart."""
    config = seeded_config
    config.briefing = {"adaptive": True, "min_cache_age": 0}
    cache_dir = config.cache_dir
    cache_dir.mkdir(parents=True)

    # Create cache + stale marker
    (cache_dir / "acme.md").write_text("# Old Briefing")
    (cache_dir / "acme.stale").write_text("1")

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath, \
         patch("agent_recall.hooks.generate_briefing") as mock_gen:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_gen.return_value = cache_dir / "acme.md"

        from agent_recall.hooks import session_start_hook
        session_start_hook()

    mock_gen.assert_called_once()


def test_session_start_stale_no_adaptive(seeded_config, capsys):
    """Stale marker but adaptive=False → clears marker, serves old cache."""
    config = seeded_config
    config.briefing = {"adaptive": False}
    cache_dir = config.cache_dir
    cache_dir.mkdir(parents=True)

    (cache_dir / "acme.md").write_text("# Old Briefing")
    (cache_dir / "acme.stale").write_text("1")

    with patch("agent_recall.hooks.load_config", return_value=config), \
         patch("agent_recall.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"

        from agent_recall.hooks import session_start_hook
        session_start_hook()

    # Stale cleared, old briefing served
    assert not (cache_dir / "acme.stale").exists()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Old Briefing" in data["additionalContext"]
