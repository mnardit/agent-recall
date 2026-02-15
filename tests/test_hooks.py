"""Tests for Claude Code hooks — SessionStart and PostToolUse."""
import json
import pytest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from claude_memory.config import MemoryConfig
from claude_memory.store import MemoryStore
from claude_memory.hooks import WRITE_TOOLS


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

    with patch("claude_memory.hooks.load_config", return_value=config), \
         patch("claude_memory.hooks.Path") as MockPath:
        # Mock cwd to return "acme" as slug
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_cwd.parent.name = "projects"

        from claude_memory.hooks import session_start_hook
        session_start_hook()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Cached Briefing" in data["additionalContext"]


def test_session_start_raw_fallback(seeded_config, capsys):
    """Hook falls back to raw context when no cache."""
    config = seeded_config

    with patch("claude_memory.hooks.load_config", return_value=config), \
         patch("claude_memory.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "acme"
        mock_cwd.parent.name = "projects"
        # read_cache needs real Path for cache_dir
        with patch("claude_memory.hooks.read_cache", return_value=None):
            from claude_memory.hooks import session_start_hook
            session_start_hook()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "Memory Context" in data["additionalContext"]
    assert "Alice" in data["additionalContext"]


def test_session_start_tier0_silent(seeded_config, capsys):
    """Tier 0 agents produce no output."""
    config = seeded_config
    config.tiers = {0: ["infra-bot"]}

    with patch("claude_memory.hooks.load_config", return_value=config), \
         patch("claude_memory.hooks.Path") as MockPath:
        mock_cwd = MockPath.cwd.return_value
        mock_cwd.name = "infra-bot"
        mock_cwd.parent.name = "projects"

        from claude_memory.hooks import session_start_hook
        session_start_hook()

    captured = capsys.readouterr()
    assert captured.out == ""


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
    with patch("claude_memory.hooks.load_config", return_value=config), \
         patch("sys.stdin", StringIO(stdin_data)), \
         patch("claude_memory.hooks.generate_vault") as mock_gen:
        from claude_memory.hooks import post_tool_use_hook
        post_tool_use_hook()
    mock_gen.assert_not_called()
