"""Tests for AI context generation — templates, cache, orchestrator/topic assembly."""
import time
import pytest
from pathlib import Path

from claude_memory.store import MemoryStore
from claude_memory.config import MemoryConfig
from claude_memory.context_gen import (
    BUILTIN_TEMPLATES, AGENT_TYPES, load_template, build_prompt,
    is_cache_fresh, get_cache_path, read_cache,
    invalidate_cache, clear_stale_marker, scope_to_agents,
    get_agent_status, get_all_statuses, get_generation_logs, LLMResult,
    _load_context_files,
    _assemble_orchestrator_context, _assemble_topic_context,
    generate_briefing, generate_all,
    DEFAULT_OUTPUT_BUDGET,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path):
    return MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        hierarchy={"acme": ["proj-a", "proj-b"]},
        tiers={0: ["infra-bot"], 2: ["acme", "proj-a", "proj-b"]},
        agent_types={
            "orchestrator": ["boss"],
            "system": ["dashboard"],
        },
        briefing={"model": "haiku", "timeout": 30},
    )


def _fake_llm(prompt: str, model: str = "opus", timeout: int = 300) -> str:
    """Fake LLM that returns a deterministic briefing."""
    return f"## Briefing\nGenerated for model={model}.\nPrompt length: {len(prompt)}"


# --- Template loading ---

def test_builtin_templates_exist():
    for agent_type in AGENT_TYPES:
        assert agent_type in BUILTIN_TEMPLATES


def test_builtin_templates_have_placeholders():
    for agent_type, template in BUILTIN_TEMPLATES.items():
        assert "{slug}" in template
        assert "{raw_context}" in template
        assert "{budget}" in template


def test_load_template_builtin():
    template = load_template("client")
    assert "{slug}" in template
    assert "client" in template.lower() or "Key People" in template


def test_load_template_from_file(tmp_path):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "client.md").write_text("Custom: {slug} {raw_context} {budget}")
    template = load_template("client", templates_dir)
    assert template == "Custom: {slug} {raw_context} {budget}"


def test_load_template_fallback_to_builtin(tmp_path):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    # No file for "agency" — should fall back to builtin
    template = load_template("agency", templates_dir)
    assert template == BUILTIN_TEMPLATES["agency"]


def test_load_template_unknown_type():
    template = load_template("nonexistent_type")
    assert template == BUILTIN_TEMPLATES["personal"]


# --- Prompt building ---

def test_build_prompt_basic():
    prompt = build_prompt("my-agent", "personal", "raw data here")
    assert "my-agent" in prompt
    assert "raw data here" in prompt
    assert str(DEFAULT_OUTPUT_BUDGET) in prompt


def test_build_prompt_custom_budget():
    prompt = build_prompt("my-agent", "client", "data", output_budget=5000)
    assert "5000" in prompt


def test_build_prompt_with_templates_dir(tmp_path):
    templates_dir = tmp_path / "tpl"
    templates_dir.mkdir()
    (templates_dir / "client.md").write_text("Agent {slug}: {raw_context} (max {budget})")
    prompt = build_prompt("test", "client", "my data",
                          output_budget=1000, templates_dir=templates_dir)
    assert prompt == "Agent test: my data (max 1000)"


# --- Cache management ---

def test_cache_fresh(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "test-agent.md").write_text("cached content")
    assert is_cache_fresh("test-agent", cache_dir) is True


def test_cache_stale(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    path = cache_dir / "test-agent.md"
    path.write_text("old content")
    # Set mtime to 2 days ago
    old_time = time.time() - 172800
    import os
    os.utime(path, (old_time, old_time))
    assert is_cache_fresh("test-agent", cache_dir) is False


def test_cache_missing(tmp_path):
    assert is_cache_fresh("nonexistent", tmp_path) is False


def test_get_cache_path(tmp_path):
    path = get_cache_path("my-agent", tmp_path)
    assert path == tmp_path / "my-agent.md"


def test_read_cache_fresh(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "agent.md").write_text("briefing content")
    assert read_cache("agent", cache_dir) == "briefing content"


def test_read_cache_stale(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    path = cache_dir / "agent.md"
    path.write_text("old")
    import os
    os.utime(path, (0, 0))
    assert read_cache("agent", cache_dir) is None


# --- Cache invalidation (adaptive mode) ---

def test_invalidate_cache_creates_stale_marker(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "agent-a.md").write_text("cached")
    (cache_dir / "agent-b.md").write_text("cached")

    result = invalidate_cache(["agent-a", "agent-b"], cache_dir)
    assert result == ["agent-a", "agent-b"]
    assert (cache_dir / "agent-a.stale").exists()
    assert (cache_dir / "agent-b.stale").exists()


def test_invalidate_cache_skips_missing(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # No cache file for "missing" — should not create stale marker
    result = invalidate_cache(["missing"], cache_dir)
    assert result == []
    assert not (cache_dir / "missing.stale").exists()


def test_stale_marker_makes_cache_not_fresh(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "agent.md").write_text("cached")
    assert is_cache_fresh("agent", cache_dir) is True

    # Mark as stale
    (cache_dir / "agent.stale").write_text("1")
    assert is_cache_fresh("agent", cache_dir) is False


def test_clear_stale_marker(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "agent.stale").write_text("1")
    clear_stale_marker("agent", cache_dir)
    assert not (cache_dir / "agent.stale").exists()


def test_generate_briefing_clears_stale(tmp_path, config):
    """Regenerating a briefing clears the stale marker."""
    _seed_enough_data(config)
    cache_dir = config.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Create initial cache + stale marker
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    (cache_dir / "acme.stale").write_text("1")
    assert is_cache_fresh("acme", cache_dir) is False

    # Regenerate — should clear stale
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    assert not (cache_dir / "acme.stale").exists()
    assert is_cache_fresh("acme", cache_dir) is True


def test_scope_to_agents_direct(config):
    """Writing to scope returns the agent itself."""
    affected = scope_to_agents("acme", config)
    assert "acme" in affected


def test_scope_to_agents_parent(config):
    """Writing to a child scope also invalidates the parent."""
    affected = scope_to_agents("proj-a", config)
    assert "proj-a" in affected
    assert "acme" in affected  # parent


def test_scope_to_agents_orchestrator(config):
    """Orchestrator agents always in affected set."""
    affected = scope_to_agents("proj-a", config)
    assert "boss" in affected


def test_scope_to_agents_unknown_scope(config):
    """Unknown scope — only orchestrator affected."""
    affected = scope_to_agents("random-scope", config)
    assert "boss" in affected
    assert "random-scope" not in affected  # not a known agent


# --- Context file loading ---

def test_load_context_files_basic(tmp_path):
    f1 = tmp_path / "file1.md"
    f1.write_text("Content of file 1")
    f2 = tmp_path / "file2.md"
    f2.write_text("Content of file 2")

    result = _load_context_files([f1, f2], budget=5000)
    assert "file1.md" in result
    assert "Content of file 1" in result
    assert "file2.md" in result


def test_load_context_files_budget_truncation(tmp_path):
    f1 = tmp_path / "big.md"
    f1.write_text("x" * 500)

    result = _load_context_files([f1], budget=100)
    assert "truncated" in result
    assert len(result) < 200  # header + 100 chars + truncation notice


def test_load_context_files_missing_skipped(tmp_path):
    f1 = tmp_path / "exists.md"
    f1.write_text("Real content")
    missing = tmp_path / "missing.md"

    result = _load_context_files([missing, f1], budget=5000)
    assert "Real content" in result
    assert "missing" not in result


def test_generate_briefing_with_context_files(tmp_path):
    """context_files content reaches the LLM prompt."""
    ctx_file = tmp_path / "project.md"
    ctx_file.write_text("# My Project\nImportant project documentation here.")

    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        tiers={1: ["my-agent"]},
        agents_config={
            "my-agent": {
                "context_files": [str(ctx_file)],
                "context_budget": 5000,
            },
        },
        briefing={"model": "haiku", "timeout": 30},
    )
    # Empty DB — context_files alone should provide enough context
    store = MemoryStore(config.db_path)
    store.close()

    captured = {}
    def capturing_llm(prompt, model, timeout):
        captured["prompt"] = prompt
        return "## Briefing\nGenerated."

    result = generate_briefing("my-agent", config=config, force=True,
                               llm_caller=capturing_llm)
    assert result is not None
    assert "Important project documentation" in captured["prompt"]


# --- Orchestrator context assembly ---

def test_orchestrator_context_people(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "Engineer")
    store.add_observation(eid, "Good at Python")
    result = _assemble_orchestrator_context(store, budget=50000)
    assert "## People" in result
    assert "Alice" in result
    assert "Engineer" in result
    assert "Good at Python" in result


def test_orchestrator_context_clients(store):
    cid = store.resolve_entity("Acme", "client")
    store.set_slot(cid, "status", "active")
    result = _assemble_orchestrator_context(store, budget=50000)
    assert "## Clients" in result
    assert "Acme" in result


def test_orchestrator_context_topics(store):
    tid = store.resolve_entity("my-topic", "topic")
    store.set_slot(tid, "status", "open")
    store.set_slot(tid, "parent_project", "acme")
    result = _assemble_orchestrator_context(store, budget=50000)
    assert "## Topics" in result
    assert "my-topic" in result


def test_orchestrator_context_relations(store):
    a = store.resolve_entity("Alice", "person")
    store.set_slot(a, "role", "Dev")
    b = store.resolve_entity("Acme", "client")
    store.set_slot(b, "status", "active")
    store.add_relation(a, b, "works_at")
    result = _assemble_orchestrator_context(store, budget=50000)
    assert "## Relations" in result
    assert "works_at" in result


def test_orchestrator_context_budget(store):
    for i in range(100):
        eid = store.resolve_entity(f"Person{i:03d}", "person")
        store.set_slot(eid, "role", f"Role {i}" * 20)
    result = _assemble_orchestrator_context(store, budget=500)
    assert len(result) <= 500


# --- Topic context assembly ---

def test_topic_context_basic(store):
    tid = store.resolve_entity("my-topic", "topic")
    store.set_slot(tid, "status", "open", scope="global")
    store.set_slot(tid, "origin", "Testing topics")
    store.add_observation(tid, "Key observation", scope="my-topic")

    result = _assemble_topic_context(store, "my-topic",
                                      ["global", "acme", "my-topic"], 50000)
    assert "## Topic: my-topic" in result
    assert "Key observation" in result


def test_topic_context_scoped_entities(store):
    tid = store.resolve_entity("my-topic", "topic")
    store.set_slot(tid, "status", "open")
    # Person with data in topic scope
    pid = store.resolve_entity("Alice", "person")
    store.set_slot(pid, "role", "Dev", scope="my-topic")
    store.add_observation(pid, "Assigned to topic", scope="my-topic")

    result = _assemble_topic_context(store, "my-topic",
                                      ["global", "acme", "my-topic"], 50000)
    assert "Alice" in result
    assert "Assigned to topic" in result


def test_topic_context_parent_entities(store):
    tid = store.resolve_entity("my-topic", "topic")
    store.set_slot(tid, "status", "open")
    # Person in parent scope
    pid = store.resolve_entity("Bob", "person")
    store.set_slot(pid, "role", "Manager", scope="acme")

    result = _assemble_topic_context(store, "my-topic",
                                      ["global", "acme", "my-topic"], 50000)
    assert "Bob" in result


# --- generate_briefing ---

def _seed_enough_data(config, scope="acme"):
    """Seed enough data to pass the 50-char minimum raw context threshold."""
    store = MemoryStore(config.db_path)
    for name, role in [("Alice", "Senior Engineer"), ("Bob", "Product Manager"),
                       ("Carol", "Designer")]:
        eid = store.resolve_entity(name, "person")
        store.set_slot(eid, "role", role, scope=scope)
        store.set_slot(eid, "email", f"{name.lower()}@example.com", scope=scope)
        store.add_observation(eid, f"{name} is a key team member", scope=scope)
    cid = store.resolve_entity("Acme Corp", "client")
    store.set_slot(cid, "status", "active", scope=scope)
    store.close()


def test_generate_briefing_basic(tmp_path, config):
    _seed_enough_data(config)

    result = generate_briefing("acme", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is not None
    assert result.exists()
    content = result.read_text()
    assert "Briefing" in content
    assert "model=haiku" in content  # from config


def test_generate_briefing_tier0_skipped(config):
    result = generate_briefing("infra-bot", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is None


def test_generate_briefing_no_context(tmp_path, config):
    # Empty DB — no meaningful context
    result = generate_briefing("proj-a", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is None


def test_generate_briefing_uses_cache(tmp_path, config):
    _seed_enough_data(config)

    # First call generates
    p1 = generate_briefing("acme", config=config, force=True,
                           llm_caller=_fake_llm)
    assert p1 is not None

    # Second call returns cached (no LLM call needed)
    call_count = 0
    def counting_llm(prompt, model, timeout):
        nonlocal call_count
        call_count += 1
        return _fake_llm(prompt, model, timeout)

    p2 = generate_briefing("acme", config=config, force=False,
                           llm_caller=counting_llm)
    assert p2 is not None
    assert call_count == 0  # Used cache


def test_generate_briefing_orchestrator(tmp_path, config):
    _seed_enough_data(config, scope="global")

    result = generate_briefing("boss", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is not None


def test_generate_briefing_topic(tmp_path, config):
    store = MemoryStore(config.db_path)
    tid = store.resolve_entity("my-topic", "topic")
    store.set_slot(tid, "status", "open")
    store.set_slot(tid, "origin", "Test topic")
    store.add_observation(tid, "Important context about the topic", scope="my-topic")
    store.add_observation(tid, "More context to reach 50 chars minimum", scope="my-topic")
    store.close()

    result = generate_briefing("my-topic", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is not None


def test_generate_briefing_per_agent_model(tmp_path):
    """Per-agent model override reaches the LLM caller."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        hierarchy={"acme": ["proj-a"]},
        tiers={2: ["acme", "proj-a"]},
        briefing={"model": "haiku", "timeout": 60},
        agents_config={
            "acme": {"model": "opus", "timeout": 300},
        },
    )
    _seed_enough_data(config)

    captured = {}
    def capturing_llm(prompt, model, timeout):
        captured["model"] = model
        captured["timeout"] = timeout
        return "## Briefing\nGenerated."

    generate_briefing("acme", config=config, force=True, llm_caller=capturing_llm)
    assert captured["model"] == "opus"
    assert captured["timeout"] == 300

    # proj-a: no override, should use global defaults
    _seed_enough_data(config, scope="proj-a")
    generate_briefing("proj-a", config=config, force=True, llm_caller=capturing_llm)
    assert captured["model"] == "haiku"
    assert captured["timeout"] == 60


def test_generate_briefing_extra_context(tmp_path):
    """extra_context from config is appended to raw, enabling briefings for sparse agents."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        agent_types={"system": ["dashboard"]},
        extra_context={
            "dashboard": "Web dashboard for monitoring agent statuses and task lists.",
        },
        briefing={"model": "haiku", "timeout": 30},
    )
    # Seed minimal data — just enough with extra_context to pass 50-char threshold
    store = MemoryStore(config.db_path)
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "Admin", scope="dashboard")
    store.close()

    captured_prompt = {}
    def capturing_llm(prompt, model, timeout):
        captured_prompt["prompt"] = prompt
        return "## Briefing\nGenerated."

    result = generate_briefing("dashboard", config=config, force=True,
                               llm_caller=capturing_llm)
    assert result is not None
    assert "Additional Context" in captured_prompt["prompt"]
    assert "Web dashboard" in captured_prompt["prompt"]


def test_generate_briefing_extra_context_saves_sparse_agent(tmp_path):
    """Agent with no frames.db data but with extra_context still gets a briefing."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        tiers={1: ["my-service"]},
        extra_context={
            "my-service": "Background service that processes incoming webhooks. "
                          "Monitors: POST /webhooks endpoint. Retries failed deliveries.",
        },
        briefing={"model": "haiku", "timeout": 30},
    )
    # Empty DB — no entities at all
    store = MemoryStore(config.db_path)
    store.close()

    result = generate_briefing("my-service", config=config, force=True,
                               llm_caller=_fake_llm)
    assert result is not None  # Would be None without extra_context


# --- template override ---

def test_generate_briefing_template_override_inline(tmp_path):
    """Per-agent inline template changes the prompt sent to LLM."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        tiers={2: ["my-agent"]},
        agents_config={"my-agent": {"template": "Custom agent {slug}: {raw_context} (budget {budget})"}},
        briefing={"model": "haiku", "timeout": 30},
    )
    _seed_enough_data(config, scope="my-agent")
    captured = {}
    def capturing_llm(prompt, model, timeout):
        captured["prompt"] = prompt
        return "## Briefing\nGenerated."
    generate_briefing("my-agent", config=config, force=True, llm_caller=capturing_llm)
    assert captured["prompt"].startswith("Custom agent my-agent:")


def test_generate_briefing_template_type_override(tmp_path):
    """Per-agent template type selects a different builtin template."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        hierarchy={"acme": ["my-agent"]},
        tiers={2: ["acme", "my-agent"]},
        agents_config={"my-agent": {"template": "personal"}},
        briefing={"model": "haiku", "timeout": 30},
    )
    _seed_enough_data(config, scope="my-agent")
    captured = {}
    def capturing_llm(prompt, model, timeout):
        captured["prompt"] = prompt
        return "## Briefing\nGenerated."
    generate_briefing("my-agent", config=config, force=True, llm_caller=capturing_llm)
    # "my-agent" is hierarchy child → auto-detect = "client"
    # But override = "personal" → should use personal template text
    assert "Keep it brief" in captured["prompt"]


# --- enabled/disabled ---

def test_generate_briefing_disabled_agent(tmp_path):
    """Disabled agent returns None without calling LLM."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        tiers={2: ["my-agent"]},
        agents_config={"my-agent": {"enabled": False}},
        briefing={"model": "haiku", "timeout": 30},
    )
    _seed_enough_data(config, scope="my-agent")
    call_count = 0
    def counting_llm(prompt, model, timeout):
        nonlocal call_count
        call_count += 1
        return _fake_llm(prompt, model, timeout)
    result = generate_briefing("my-agent", config=config, force=True, llm_caller=counting_llm)
    assert result is None
    assert call_count == 0


def test_generate_all_skips_disabled(tmp_path):
    """generate_all reports disabled agents as skip:disabled."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        tiers={2: ["agent-a", "agent-b"]},
        agents_config={"agent-b": {"enabled": False}},
        briefing={"model": "haiku", "timeout": 30},
    )
    _seed_enough_data(config, scope="agent-a")
    results = generate_all(["agent-a", "agent-b"], config=config, force=True, llm_caller=_fake_llm)
    assert results["agent-b"] == "skip:disabled"


# --- generate_all ---

def test_generate_all_basic(tmp_path, config):
    _seed_enough_data(config)

    results = generate_all(
        agent_slugs=["acme", "proj-a", "infra-bot"],
        config=config, force=True, llm_caller=_fake_llm,
    )
    assert results["infra-bot"] == "skip:tier0"
    assert results["acme"] == "ok"


def test_generate_all_uses_config_agents(tmp_path, config):
    _seed_enough_data(config)

    results = generate_all(config=config, force=True, llm_caller=_fake_llm)
    # Should process all agents from config.all_agents()
    assert isinstance(results, dict)
    assert len(results) > 0


# --- agent status API ---

def test_get_agent_status_cached(tmp_path, config):
    """Status for agent with fresh cache."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    status = get_agent_status("acme", config)
    assert status["has_cache"] is True
    assert status["is_stale"] is False
    assert status["is_fresh"] is True
    assert status["size_bytes"] > 0
    assert status["generated_at"] is not None
    assert status["age_seconds"] < 5
    assert status["enabled"] is True
    assert status["model"] == "haiku"


def test_get_agent_status_no_cache(config):
    """Status for agent with no cache."""
    status = get_agent_status("proj-a", config)
    assert status["has_cache"] is False
    assert status["is_fresh"] is False
    assert status["size_bytes"] == 0
    assert status["generated_at"] is None


def test_get_agent_status_stale(tmp_path, config):
    """Status for agent with stale marker."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    (config.cache_dir / "acme.stale").write_text("1")
    status = get_agent_status("acme", config)
    assert status["has_cache"] is True
    assert status["is_stale"] is True
    assert status["is_fresh"] is False


def test_get_agent_status_disabled(tmp_path):
    """Status for disabled agent."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        cache_dir=tmp_path / "cache",
        agents_config={"my-agent": {"enabled": False}},
    )
    status = get_agent_status("my-agent", config)
    assert status["enabled"] is False


def test_get_agent_status_iso_timestamp(tmp_path, config):
    """Status includes ISO format timestamp."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    status = get_agent_status("acme", config)
    assert status["generated_at_iso"] is not None
    assert "T" in status["generated_at_iso"]  # ISO format


def test_get_agent_status_no_cache_iso(config):
    """No cache — ISO timestamp is None."""
    status = get_agent_status("proj-a", config)
    assert status["generated_at_iso"] is None


def test_get_all_statuses(tmp_path, config):
    """Batch status for all agents."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    statuses = get_all_statuses(config)
    assert isinstance(statuses, dict)
    assert "acme" in statuses
    assert statuses["acme"]["has_cache"] is True
    assert "boss" in statuses  # orchestrator


# --- generation logs ---

def test_generation_log_created(tmp_path, config):
    """Generating a briefing creates a log entry."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    logs = get_generation_logs("acme", config)
    assert len(logs) == 1
    entry = logs[0]
    assert entry["slug"] == "acme"
    assert entry["status"] == "ok"
    assert entry["model"] == "haiku"
    assert entry["duration_ms"] >= 0
    assert entry["input_chars"] > 0
    assert entry["output_chars"] > 0
    assert "timestamp" in entry
    assert entry["input_tokens"] is None  # str caller, no token tracking


def test_generation_log_error(tmp_path, config):
    """Failed generation logs an error."""
    _seed_enough_data(config)
    def failing_llm(prompt, model, timeout):
        return None
    generate_briefing("acme", config=config, force=True, llm_caller=failing_llm)
    logs = get_generation_logs("acme", config)
    assert len(logs) == 1
    assert logs[0]["status"] == "error:empty_response"


def test_generation_log_rotation(tmp_path, config):
    """Logs rotate — only last N entries kept."""
    _seed_enough_data(config)
    for _ in range(15):
        generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    logs = get_generation_logs("acme", config)
    assert len(logs) == 10


def test_generation_logs_empty_agent(config):
    """Agent with no logs returns empty list."""
    logs = get_generation_logs("nonexistent", config)
    assert logs == []


# --- LLMResult token tracking ---

def test_generation_log_with_tokens(tmp_path, config):
    """LLMResult includes token counts in log."""
    _seed_enough_data(config)
    def token_llm(prompt, model, timeout):
        return LLMResult(
            text="## Briefing\nGenerated.",
            input_tokens=1500,
            output_tokens=800,
        )
    generate_briefing("acme", config=config, force=True, llm_caller=token_llm)
    logs = get_generation_logs("acme", config)
    assert logs[0]["input_tokens"] == 1500
    assert logs[0]["output_tokens"] == 800
    assert logs[0]["status"] == "ok"


def test_llm_result_backward_compat(tmp_path, config):
    """Old-style str return still works with no token data."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    logs = get_generation_logs("acme", config)
    assert logs[0]["input_tokens"] is None
    assert logs[0]["output_tokens"] is None
    assert logs[0]["status"] == "ok"
