# Dashboard Integration Features

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add enabled toggle, custom templates, agent status API, generation logging, and cost tracking to claude-memory — everything the dashboard needs for full briefing management.

**Architecture:** All features live in the library (config.py, context_gen.py). Dashboard consumes them via Python imports. No dashboard code changes in this plan — only library API. Config changes are backward-compatible (new optional fields).

**Tech Stack:** Python, pytest, YAML config, pathlib

---

### Task 1: Agent enabled/disabled toggle

**Files:**
- Modify: `claude_memory/config.py` — add `get_agent_enabled(slug)` method
- Modify: `claude_memory/context_gen.py` — check enabled in `generate_briefing` and `generate_all`
- Modify: `tests/test_config.py` — test enabled parsing
- Modify: `tests/test_context_gen.py` — test skip on disabled

**Step 1: Write failing tests in test_config.py**

```python
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
```

**Step 2: Run tests — expect FAIL (method doesn't exist)**

Run: `cd /home/yahont/projects/personal/claude-memory && python3 -m pytest tests/test_config.py::test_get_agent_enabled_default tests/test_config.py::test_get_agent_enabled_false -v`

**Step 3: Implement `get_agent_enabled` in config.py**

Add to `MemoryConfig`:
```python
def get_agent_enabled(self, slug: str) -> bool:
    """Check if agent briefing generation is enabled. Default: True."""
    agent_cfg = self.agents_config.get(slug, {})
    return agent_cfg.get("enabled", True)
```

**Step 4: Run tests — expect PASS**

**Step 5: Write failing tests in test_context_gen.py**

```python
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
```

**Step 6: Implement in context_gen.py**

In `generate_briefing`, after loading config but before cache check:
```python
if not config.get_agent_enabled(slug):
    log.info("Agent %s is disabled, skipping", slug)
    return None
```

In `generate_all`, after tier0 check:
```python
if not config.get_agent_enabled(slug):
    results[slug] = "skip:disabled"
    continue
```

**Step 7: Run all tests**

Run: `cd /home/yahont/projects/personal/claude-memory && python3 -m pytest tests/ -q`

**Step 8: Commit**

```bash
git add claude_memory/config.py claude_memory/context_gen.py tests/test_config.py tests/test_context_gen.py
git commit -m "feat: agent enabled/disabled toggle"
```

---

### Task 2: Custom prompt template override

Per-agent `template` field — either a type name ("client", "personal") or inline text with `{slug}`, `{raw_context}`, `{budget}` placeholders.

**Files:**
- Modify: `claude_memory/config.py` — add `get_agent_template(slug)`
- Modify: `claude_memory/context_gen.py` — use per-agent template in `generate_briefing`
- Modify: `tests/test_config.py` — test template parsing
- Modify: `tests/test_context_gen.py` — test template override reaching LLM

**Step 1: Write failing tests in test_config.py**

```python
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
```

**Step 2: Run tests — expect FAIL**

**Step 3: Implement `get_agent_template` in config.py**

```python
def get_agent_template(self, slug: str) -> str | None:
    """Get custom template override for an agent. None means auto-detect."""
    agent_cfg = self.agents_config.get(slug, {})
    return agent_cfg.get("template")
```

**Step 4: Run config tests — PASS**

**Step 5: Write failing test in test_context_gen.py**

```python
def test_generate_briefing_template_override(tmp_path):
    """Per-agent template override changes the prompt sent to LLM."""
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
    """Per-agent template type override selects a different builtin template."""
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
    # "my-agent" is a hierarchy child → auto-detect would pick "client"
    # But template override says "personal" → should use personal template
    assert "personal" in captured["prompt"].lower() or "Keep it brief" in captured["prompt"]
```

**Step 6: Implement in context_gen.py — modify `generate_briefing`**

After determining `agent_type`, before calling `build_prompt`:
```python
# Check for per-agent template override
custom_template = config.get_agent_template(slug)
if custom_template:
    if custom_template in BUILTIN_TEMPLATES:
        # Type name override — use that builtin template
        agent_type = custom_template
    else:
        # Inline custom template — format directly
        prompt = custom_template.format(slug=slug, raw_context=raw, budget=output_budget)
```

Only call `build_prompt` if no inline custom template was used.

**Step 7: Run all tests — PASS**

**Step 8: Commit**

```bash
git commit -m "feat: per-agent prompt template override"
```

---

### Task 3: Agent status API

Function `get_agent_status(slug, config)` returning a dict with cache metadata. Dashboard calls this to show last-generated time, stale status, cache size.

**Files:**
- Modify: `claude_memory/context_gen.py` — add `get_agent_status()` function
- Modify: `tests/test_context_gen.py` — test status for cached, stale, missing agents

**Step 1: Write failing tests**

```python
def test_get_agent_status_cached(tmp_path, config):
    """Status for agent with fresh cache."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    status = get_agent_status("acme", config)
    assert status["has_cache"] is True
    assert status["is_stale"] is False
    assert status["is_fresh"] is True
    assert status["size_bytes"] > 0
    assert status["generated_at"] is not None  # float timestamp
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
```

**Step 2: Run tests — expect FAIL**

**Step 3: Implement `get_agent_status`**

```python
def get_agent_status(slug: str, config: MemoryConfig | None = None) -> dict:
    """Get cache/briefing status for an agent.

    Returns dict with: has_cache, is_fresh, is_stale, enabled, model,
    size_bytes, generated_at, age_seconds, template_type.
    """
    config = config or load_config()
    cache_dir = config.cache_dir
    cache_path = get_cache_path(slug, cache_dir)
    agent_briefing = config.get_agent_briefing(slug)
    stale_path = cache_dir / f"{slug}.stale"

    has_cache = cache_path.exists()
    generated_at = cache_path.stat().st_mtime if has_cache else None
    age = time.time() - generated_at if generated_at else None
    size = cache_path.stat().st_size if has_cache else 0

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
        "age_seconds": round(age) if age is not None else None,
    }
```

**Step 4: Run tests — PASS**

**Step 5: Add to `__init__.py` exports**

Add `get_agent_status` to the public API exports.

**Step 6: Commit**

```bash
git commit -m "feat: get_agent_status() for cache metadata queries"
```

---

### Task 4: Generation log capture

Capture timing, token estimates, errors per generation. Store in `<cache_dir>/<slug>.log.json`. Last N runs kept (configurable, default 10).

**Files:**
- Modify: `claude_memory/context_gen.py` — add `GenerationLog` dataclass, `_save_log`, `get_agent_logs`; update `generate_briefing` to record logs
- Modify: `tests/test_context_gen.py` — test log creation, reading, rotation

**Step 1: Write failing tests**

```python
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
    assert entry["duration_ms"] > 0
    assert entry["input_chars"] > 0
    assert entry["output_chars"] > 0
    assert "timestamp" in entry

def test_generation_log_error(tmp_path, config):
    """Failed generation logs an error."""
    _seed_enough_data(config)
    def failing_llm(prompt, model, timeout):
        return None  # LLM returned empty
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
    assert len(logs) == 10  # default max

def test_generation_logs_empty_agent(config):
    """Agent with no logs returns empty list."""
    logs = get_generation_logs("nonexistent", config)
    assert logs == []
```

**Step 2: Run tests — FAIL**

**Step 3: Implement log capture in context_gen.py**

```python
import json

MAX_LOG_ENTRIES = 10

def _save_generation_log(slug: str, entry: dict, cache_dir: Path,
                         max_entries: int = MAX_LOG_ENTRIES) -> None:
    """Append a log entry to <cache_dir>/<slug>.log.json, rotating old entries."""
    log_path = cache_dir / f"{slug}.log.json"
    entries = []
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text())
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    entries = entries[-max_entries:]
    log_path.write_text(json.dumps(entries, indent=2))

def get_generation_logs(slug: str, config: MemoryConfig | None = None) -> list[dict]:
    """Read generation log entries for an agent."""
    config = config or load_config()
    log_path = config.cache_dir / f"{slug}.log.json"
    if not log_path.exists():
        return []
    try:
        return json.loads(log_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
```

Update `generate_briefing` — wrap the LLM call + cache write with timing:
```python
import time as _time

start = _time.time()
result = caller(prompt, model, timeout)
duration_ms = int((_time.time() - start) * 1000)

log_entry = {
    "slug": slug,
    "timestamp": _time.time(),
    "model": model,
    "duration_ms": duration_ms,
    "input_chars": len(prompt),
    "output_chars": len(result) if result else 0,
    "agent_type": agent_type,
}

if not result:
    log_entry["status"] = "error:empty_response"
    _save_generation_log(slug, log_entry, cache_dir)
    return None

log_entry["status"] = "ok"
# ... write cache ...
_save_generation_log(slug, log_entry, cache_dir)
```

**Step 4: Run all tests — PASS**

**Step 5: Add `get_generation_logs` to `__init__.py` exports**

**Step 6: Commit**

```bash
git commit -m "feat: generation log capture with rotation"
```

---

### Task 5: Cost/token tracking

Extend `LLMCaller` to optionally return token counts. Add `LLMResult` dataclass for structured return. Backward-compatible — old callers returning `str | None` still work.

**Files:**
- Modify: `claude_memory/context_gen.py` — add `LLMResult`, update log entries with token data
- Modify: `tests/test_context_gen.py` — test token tracking

**Step 1: Write failing tests**

```python
def test_generation_log_with_tokens(tmp_path, config):
    """LLM caller returning LLMResult includes token counts in log."""
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

def test_llm_result_backward_compat(tmp_path, config):
    """Old-style str return still works."""
    _seed_enough_data(config)
    generate_briefing("acme", config=config, force=True, llm_caller=_fake_llm)
    logs = get_generation_logs("acme", config)
    assert logs[0]["input_tokens"] is None
    assert logs[0]["output_tokens"] is None
    assert logs[0]["status"] == "ok"
```

**Step 2: Run tests — FAIL**

**Step 3: Implement LLMResult**

```python
@dataclass
class LLMResult:
    """Structured result from LLM invocation with optional token counts."""
    text: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
```

Update `generate_briefing` — handle both str and LLMResult returns:
```python
raw_result = caller(prompt, model, timeout)

# Normalize — support both str|None and LLMResult
if isinstance(raw_result, LLMResult):
    result = raw_result.text
    input_tokens = raw_result.input_tokens
    output_tokens = raw_result.output_tokens
else:
    result = raw_result
    input_tokens = None
    output_tokens = None

log_entry = {
    ...
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
}
```

**Step 4: Run all tests — PASS**

**Step 5: Add `LLMResult` to `__init__.py` exports**

**Step 6: Commit**

```bash
git commit -m "feat: LLMResult with token tracking for cost analysis"
```

---

### Task 6: Public API surface + exports

Ensure all new functions are properly exported, update `__init__.py` and verify import ergonomics.

**Files:**
- Modify: `claude_memory/__init__.py` — add new exports
- Modify: `tests/test_context_gen.py` — verify imports at top (already done per task)

**Step 1: Read current `__init__.py`**

**Step 2: Add new exports**

```python
from claude_memory.context_gen import (
    get_agent_status,
    get_generation_logs,
    LLMResult,
)
```

**Step 3: Write import test**

```python
def test_public_api_exports():
    from claude_memory import (
        get_agent_status,
        get_generation_logs,
        LLMResult,
    )
    assert callable(get_agent_status)
    assert callable(get_generation_logs)
```

**Step 4: Run all tests — PASS**

**Step 5: Commit and push**

```bash
git commit -m "feat: export new API functions"
git push
```

---

## Summary

| Task | Feature | Config key | API function |
|------|---------|------------|--------------|
| 1 | Enabled toggle | `agents.<slug>.enabled` | `config.get_agent_enabled(slug)` |
| 2 | Template override | `agents.<slug>.template` | `config.get_agent_template(slug)` |
| 3 | Agent status | (read-only) | `get_agent_status(slug, config)` |
| 4 | Generation logs | (auto, in cache_dir) | `get_generation_logs(slug, config)` |
| 5 | Token tracking | (via LLMResult) | `LLMResult` dataclass |
| 6 | Public API | (exports) | in `__init__.py` |

Dashboard integration: all these functions are importable. Dashboard already has `GET/POST /api/memory/briefing-config` and `GET /api/agents/{id}/context`. New endpoints needed in dashboard (not in this plan): `GET /api/agents/{id}/context/status` (calls `get_agent_status`) and `GET /api/agents/{id}/context/logs` (calls `get_generation_logs`).
