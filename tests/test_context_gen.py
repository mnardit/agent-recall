"""Tests for AI context generation — templates, cache, orchestrator/topic assembly."""
import time
import pytest
from pathlib import Path

from claude_memory.store import MemoryStore
from claude_memory.config import MemoryConfig
from claude_memory.context_gen import (
    BUILTIN_TEMPLATES, AGENT_TYPES, load_template, build_prompt,
    is_cache_fresh, get_cache_path, read_cache,
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
