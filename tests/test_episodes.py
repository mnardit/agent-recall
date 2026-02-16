import pytest
from pathlib import Path
from agent_memory.episodes import save_episode, load_recent_episodes


@pytest.fixture
def episodes_dir(tmp_path):
    return tmp_path / "episodes"


def test_save_episode(episodes_dir):
    path = save_episode(episodes_dir, project="my-project", session_id="abc123",
                        summary="Completed audit", decisions=["Use SQLite"], open_items=["Add cascade"])
    assert path.exists()
    assert "audit" in path.read_text()


def test_load_recent(episodes_dir):
    save_episode(episodes_dir, project="my-project", session_id="s1",
                 summary="S1", decisions=[], open_items=[])
    save_episode(episodes_dir, project="my-project", session_id="s2",
                 summary="S2", decisions=[], open_items=[])
    assert len(load_recent_episodes(episodes_dir, days=3)) == 2


def test_episode_markdown_format(episodes_dir):
    path = save_episode(episodes_dir, project="client-a", session_id="xyz",
                        summary="Configured ads",
                        decisions=["Budget 500"], open_items=["Landing page"])
    content = path.read_text()
    assert "## Summary" in content
    assert "## Decisions" in content
    assert "## Open" in content
