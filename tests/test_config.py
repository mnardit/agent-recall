import pytest
from pathlib import Path
from agent_recall.config import MemoryConfig, load_config


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


def test_load_config_malformed_yaml(tmp_path):
    """Malformed YAML raises ValueError with file path."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: [unclosed\n  broken: yaml")
    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(bad)


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


def test_agents_config_parsed(tmp_path):
    """agents section parsed with per-agent overrides."""
    (tmp_path / "memory.yaml").write_text("""\
briefing:
  model: haiku
  timeout: 120
agents:
  boss:
    model: opus
    timeout: 300
    output_budget: 12000
  dashboard:
    model: haiku
    extra_context: "Dashboard context"
    context_files:
      - ~/some/file.md
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.agents_config["boss"]["model"] == "opus"
    assert config.agents_config["dashboard"]["extra_context"] == "Dashboard context"


def test_get_agent_briefing_merges(tmp_path):
    """Per-agent settings override global defaults."""
    (tmp_path / "memory.yaml").write_text("""\
briefing:
  model: haiku
  timeout: 120
  output_budget: 8000
agents:
  boss:
    model: opus
    timeout: 300
""")
    config = load_config(tmp_path / "memory.yaml")
    # boss: overrides model + timeout, inherits output_budget
    b = config.get_agent_briefing("boss")
    assert b["model"] == "opus"
    assert b["timeout"] == 300
    assert b["output_budget"] == 8000
    # unknown agent: gets global defaults
    d = config.get_agent_briefing("random")
    assert d["model"] == "haiku"
    assert d["timeout"] == 120


def test_get_agent_briefing_merges_all_keys(tmp_path):
    """min_cache_age and adaptive are also merged per-agent."""
    (tmp_path / "memory.yaml").write_text("""\
briefing:
  model: haiku
  adaptive: true
  min_cache_age: 1800
agents:
  special:
    adaptive: false
    min_cache_age: 0
""")
    config = load_config(tmp_path / "memory.yaml")
    b = config.get_agent_briefing("special")
    assert b["adaptive"] is False
    assert b["min_cache_age"] == 0
    # Global still has original values
    g = config.get_agent_briefing("other")
    assert g["adaptive"] is True
    assert g["min_cache_age"] == 1800


def test_get_agent_extra_context_from_agents_section(tmp_path):
    """extra_context in agents section takes precedence over top-level."""
    (tmp_path / "memory.yaml").write_text("""\
extra_context:
  dashboard: "Old top-level context"
agents:
  dashboard:
    extra_context: "New per-agent context"
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.get_agent_extra_context("dashboard") == "New per-agent context"


def test_get_agent_extra_context_fallback(tmp_path):
    """Falls back to top-level extra_context if not in agents section."""
    (tmp_path / "memory.yaml").write_text("""\
extra_context:
  dashboard: "Top-level context"
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.get_agent_extra_context("dashboard") == "Top-level context"
    assert config.get_agent_extra_context("nonexistent") is None


def test_get_agent_context_files(tmp_path):
    """context_files parsed and expanded."""
    (tmp_path / "memory.yaml").write_text("""\
agents:
  dashboard:
    context_files:
      - /tmp/file1.md
      - /tmp/file2.md
    context_budget: 5000
""")
    config = load_config(tmp_path / "memory.yaml")
    files = config.get_agent_context_files("dashboard")
    assert len(files) == 2
    assert files[0] == Path("/tmp/file1.md")
    assert config.get_agent_context_budget("dashboard") == 5000
    # Default budget for unknown agent
    assert config.get_agent_context_budget("unknown") == 8000
    # No context_files for unknown agent
    assert config.get_agent_context_files("unknown") == []


def test_get_agent_enabled_default(config):
    """Agents are enabled by default."""
    assert config.get_agent_enabled("client-a") is True


def test_get_agent_enabled_false(tmp_path):
    """Agent with enabled: false is disabled."""
    (tmp_path / "memory.yaml").write_text("""\
agents:
  my-agent:
    enabled: false
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.get_agent_enabled("my-agent") is False


def test_get_agent_template_default(config):
    """No template override returns None (auto-detect)."""
    assert config.get_agent_template("client-a") is None


def test_get_agent_template_type_override(tmp_path):
    """template as type name string."""
    (tmp_path / "memory.yaml").write_text("""\
agents:
  my-agent:
    template: personal
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.get_agent_template("my-agent") == "personal"


def test_get_agent_template_custom_text(tmp_path):
    """template as inline text with placeholders."""
    (tmp_path / "memory.yaml").write_text("""\
agents:
  my-agent:
    template: "Custom {slug}: {raw_context} (max {budget})"
""")
    config = load_config(tmp_path / "memory.yaml")
    assert "Custom {slug}" in config.get_agent_template("my-agent")


def test_hierarchy_shorthand(tmp_path):
    """Hierarchy can be specified as parent: [children] shorthand."""
    (tmp_path / "memory.yaml").write_text("""\
hierarchy:
  acme: [client-a, client-b]
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.hierarchy["acme"] == ["client-a", "client-b"]


# --- known_scopes ---

def test_known_scopes_includes_hierarchy(config):
    """known_scopes() includes all hierarchy parents and children."""
    scopes = config.known_scopes()
    assert "global" in scopes
    assert "acme" in scopes
    assert "client-a" in scopes
    assert "client-b" in scopes
    assert "other-org" in scopes
    assert "project-x" in scopes


def test_known_scopes_includes_tiers(config):
    """known_scopes() includes slugs from all tiers."""
    scopes = config.known_scopes()
    assert "sync-bot" in scopes
    assert "data-fetcher" in scopes
    assert "dashboard" in scopes


def test_known_scopes_includes_agent_types(config):
    """known_scopes() includes slugs from agent_types."""
    scopes = config.known_scopes()
    assert "boss" in scopes


def test_known_scopes_empty_config():
    """Empty config returns just {'global'}."""
    config = MemoryConfig()
    assert config.known_scopes() == {"global"}


# --- get_agent warning ---

def test_get_agent_warns_on_fallback(config, caplog):
    """Unknown slug triggers a warning via logging."""
    import logging
    with caplog.at_level(logging.WARNING, logger="agent_recall"):
        agent = config.get_agent("totally-unknown")
    assert agent.chain == ["global", "totally-unknown"]
    assert "Unknown slug" in caplog.text
    assert "totally-unknown" in caplog.text


def test_get_agent_no_warning_for_known(config, caplog):
    """Known slugs (hierarchy child) do NOT trigger warning."""
    import logging
    with caplog.at_level(logging.WARNING, logger="agent_recall"):
        config.get_agent("client-a")
    assert "Unknown slug" not in caplog.text


# --- Env var resolution ---

def test_env_var_db_path(tmp_path, monkeypatch):
    """AGENT_RECALL_DB_PATH overrides default when no config file."""
    custom_db = tmp_path / "custom" / "frames.db"
    monkeypatch.setenv("AGENT_RECALL_DB_PATH", str(custom_db))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config()
    assert config.db_path == custom_db


def test_env_var_cache_dir(tmp_path, monkeypatch):
    """AGENT_RECALL_CACHE_DIR overrides default when no config file."""
    custom_cache = tmp_path / "custom_cache"
    monkeypatch.setenv("AGENT_RECALL_CACHE_DIR", str(custom_cache))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config()
    assert config.cache_dir == custom_cache


def test_yaml_overrides_env_var(tmp_path, monkeypatch):
    """YAML config db_path takes precedence over env var."""
    yaml_db = tmp_path / "yaml.db"
    env_db = tmp_path / "env.db"
    monkeypatch.setenv("AGENT_RECALL_DB_PATH", str(env_db))
    cfg_file = tmp_path / "memory.yaml"
    cfg_file.write_text(f"db_path: {yaml_db}\n")
    config = load_config(cfg_file)
    assert config.db_path == yaml_db


def test_env_var_with_yaml_no_db_path(tmp_path, monkeypatch):
    """Env var fills in when YAML exists but has no db_path."""
    env_db = tmp_path / "env.db"
    monkeypatch.setenv("AGENT_RECALL_DB_PATH", str(env_db))
    cfg_file = tmp_path / "memory.yaml"
    cfg_file.write_text("briefing:\n  model: haiku\n")
    config = load_config(cfg_file)
    assert config.db_path == env_db


# --- Explicit scope_chain in agents config ---

def test_get_agent_explicit_scope_chain(tmp_path):
    """Explicit scope_chain in agents section overrides inferred chain."""
    (tmp_path / "memory.yaml").write_text("""\
hierarchy:
  acme: [client-a, client-b]
agents:
  client-a:
    scope_chain: ["global", "acme", "special-scope", "client-a"]
""")
    config = load_config(tmp_path / "memory.yaml")
    agent = config.get_agent("client-a")
    assert agent.chain == ["global", "acme", "special-scope", "client-a"]
    assert agent.tier == 2  # default


def test_get_agent_explicit_scope_chain_with_tier(tmp_path):
    """Explicit scope_chain respects tier from tiers dict."""
    (tmp_path / "memory.yaml").write_text("""\
tiers:
  1: [my-agent]
agents:
  my-agent:
    scope_chain: ["global", "custom"]
""")
    config = load_config(tmp_path / "memory.yaml")
    agent = config.get_agent("my-agent")
    assert agent.chain == ["global", "custom"]
    assert agent.tier == 1


def test_get_agent_explicit_scope_chain_orchestrator(tmp_path):
    """Explicit scope_chain on orchestrator agent uses tier 3."""
    (tmp_path / "memory.yaml").write_text("""\
agent_types:
  orchestrator: [boss]
agents:
  boss:
    scope_chain: ["global", "everything"]
""")
    config = load_config(tmp_path / "memory.yaml")
    agent = config.get_agent("boss")
    assert agent.chain == ["global", "everything"]
    assert agent.tier == 3
    assert agent.agent_type == "orchestrator"


def test_get_agent_explicit_scope_chain_unknown_agent(tmp_path):
    """Explicit scope_chain works for agents not in hierarchy/tiers."""
    (tmp_path / "memory.yaml").write_text("""\
agents:
  custom-agent:
    scope_chain: ["global", "acme"]
""")
    config = load_config(tmp_path / "memory.yaml")
    agent = config.get_agent("custom-agent")
    assert agent.chain == ["global", "acme"]
    assert agent.tier == 2


# --- database alias for db_path ---

def test_database_alias(tmp_path):
    """'database' key is accepted as alias for 'db_path'."""
    (tmp_path / "memory.yaml").write_text("""\
database: /tmp/test/my.db
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.db_path == Path("/tmp/test/my.db")


def test_db_path_takes_precedence_over_database(tmp_path):
    """'db_path' takes precedence when both are specified."""
    (tmp_path / "memory.yaml").write_text("""\
db_path: /tmp/primary.db
database: /tmp/alias.db
""")
    config = load_config(tmp_path / "memory.yaml")
    assert config.db_path == Path("/tmp/primary.db")
