import pytest
from pathlib import Path
from claude_memory.config import MemoryConfig, load_config, AgentConfig


YAML_CONTENT = """\
db_path: /tmp/test/frames.db
cache_dir: /tmp/test/cache

hierarchy:
  acme:
    children: [client-a, client-b]
  other-org:
    children: [project-x]

tiers:
  0: [sync-bot, data-fetcher]
  1: [dashboard]

agent_types:
  system: [dashboard, sync-bot]
  orchestrator: [boss]

briefing:
  model: haiku
  timeout: 120

vault:
  dir: /tmp/test/vault
  task_header: "## Tasks"
  auto_commit: false
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "memory.yaml"
    path.write_text(YAML_CONTENT)
    return path


@pytest.fixture
def config(config_file):
    return load_config(config_file)


def test_load_config_paths(config):
    assert config.db_path == Path("/tmp/test/frames.db")
    assert config.cache_dir == Path("/tmp/test/cache")


def test_load_hierarchy(config):
    assert config.hierarchy["acme"] == ["client-a", "client-b"]
    assert config.hierarchy["other-org"] == ["project-x"]


def test_load_tiers(config):
    assert "sync-bot" in config.tiers[0]
    assert "dashboard" in config.tiers[1]


def test_load_agent_types(config):
    assert "boss" in config.agent_types["orchestrator"]
    assert "dashboard" in config.agent_types["system"]


def test_load_briefing(config):
    assert config.briefing["model"] == "haiku"
    assert config.briefing["timeout"] == 120


def test_load_vault(config):
    assert config.vault_dir == Path("/tmp/test/vault")
    assert config.vault_task_header == "## Tasks"
    assert config.vault_auto_commit is False


def test_get_agent_orchestrator(config):
    agent = config.get_agent("boss")
    assert agent.tier == 3
    assert agent.chain == ["global"]
    assert agent.agent_type == "orchestrator"


def test_get_agent_tier0(config):
    agent = config.get_agent("sync-bot")
    assert agent.tier == 0
    assert agent.chain == []


def test_get_agent_tier1(config):
    agent = config.get_agent("dashboard")
    assert agent.tier == 1
    assert agent.chain == ["global", "dashboard"]


def test_get_agent_hierarchy_child(config):
    agent = config.get_agent("client-a")
    assert agent.tier == 2
    assert agent.chain == ["global", "acme", "client-a"]


def test_get_agent_hierarchy_parent(config):
    agent = config.get_agent("acme")
    assert agent.tier == 2
    assert agent.chain == ["global", "acme"]


def test_get_agent_unknown(config):
    agent = config.get_agent("random-agent")
    assert agent.tier == 2
    assert agent.chain == ["global", "random-agent"]


def test_get_agent_type_orchestrator(config):
    assert config.get_agent_type("boss") == "orchestrator"


def test_get_agent_type_system(config):
    assert config.get_agent_type("dashboard") == "system"


def test_get_agent_type_client(config):
    assert config.get_agent_type("client-a") == "client"


def test_get_agent_type_agency(config):
    assert config.get_agent_type("acme") == "agency"


def test_get_agent_type_personal(config):
    assert config.get_agent_type("random-agent") == "personal"


def test_scope_children(config):
    children = config.scope_children("acme")
    assert children == {"client-a", "client-b"}
    assert config.scope_children("nonexistent") == set()


def test_all_agents(config):
    agents = config.all_agents()
    assert "boss" in agents
    assert "sync-bot" in agents
    assert "client-a" in agents
    assert "acme" in agents
    assert "dashboard" in agents


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path.yaml")


def test_load_config_defaults(tmp_path, monkeypatch):
    """No config file found — returns defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config()
    assert config.hierarchy == {}
    assert config.tiers == {}


def test_load_extra_context(tmp_path):
    """extra_context loaded as per-agent dict."""
    (tmp_path / "memory.yaml").write_text("""\
extra_context:
  dashboard: |
    Web dashboard for agent monitoring.
    Service: dashboard.service
  sync-bot: "Data sync utility"
""")
    config = load_config(tmp_path / "memory.yaml")
    assert "Web dashboard" in config.extra_context["dashboard"]
    assert config.extra_context["sync-bot"] == "Data sync utility"
    assert "nonexistent" not in config.extra_context


def test_extra_context_default_empty(config):
    """extra_context defaults to empty dict when not in config."""
    assert config.extra_context == {}


def test_hierarchy_shorthand(tmp_path):
    """Hierarchy can be specified as parent: [children] shorthand."""
    (tmp_path / "memory.yaml").write_text("""\
hierarchy:
  acme: [client-a, client-b]
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.hierarchy["acme"] == ["client-a", "client-b"]
