# Quality Audit (no overengineering)

Date: 2026-02-16
Scope: repository-wide lightweight audit (architecture, tests, reliability, DX).

## What was checked

- Project structure and dependency config (`pyproject.toml`).
- Core modules for storage, context assembly, MCP bridge, and hierarchy.
- Existing automated test suite.
- Basic maintainability signals (module size and decomposition).

## Commands run

```bash
pytest -q
PYTHONPATH=. pytest -q
PYTHONPATH=. pytest --maxfail=1 --disable-warnings --cov=agent_memory --cov-report=term-missing
python - <<'PY'
from pathlib import Path
root=Path('.')
py=list(root.glob('agent_memory/*.py'))
test=list(root.glob('tests/test_*.py'))
print('modules',len(py))
print('tests',len(test))
for p in py:
    lines=p.read_text().count('\n')+1
    print(f'{p}: {lines}')
PY
```

## Snapshot assessment

## ✅ Strengths

1. **High automated test coverage breadth (by count)**  
   The suite currently contains many tests and runs green when package path is set correctly (`259 passed`).

2. **Clear layering in core memory logic**  
   The code keeps storage concerns in `MemoryStore`, scope views in `ScopedView`, and protocol adaptation in `MCPBridge`, which is a good separation baseline.

3. **Practical resilience in LLM path**  
   Context generation handles timeout / missing CLI gracefully and falls back instead of crashing.

4. **Operationally sensible defaults**  
   SQLite WAL + FK enabled by default; this is a good default for single-host agent workloads.

## ⚠️ Main risks (priority order)

1. **Developer experience friction in test execution**  
   `pytest -q` fails in a fresh checkout because `agent_memory` is not importable without either installation or `PYTHONPATH=.`, which can cause false CI/local failures.

2. **No built-in static quality gates configured in project config**  
   There is pytest config, but no configured lint/type checks in `pyproject.toml` (e.g., ruff/mypy/pyright). This increases risk of style drift and type regressions over time.

3. **Large, multi-responsibility modules**  
   `agent_memory/context_gen.py` (~810 LOC) and `agent_memory/store.py` (~595 LOC) are substantial. This is still manageable now, but likely to become the first maintainability bottleneck as features grow.

4. **Coverage plugin absent in current environment**  
   `pytest --cov ...` is currently unavailable (plugin not present), so quantitative coverage trending is not easily enforceable in this environment.

## Minimal, non-overengineered improvement plan

1. **Fix test bootstrap ergonomics (highest value / lowest effort)**
   - Document a canonical local test command in `README.md` (or Make target) that always works.
   - Optionally add editable install instructions (`pip install -e .[dev]`).

2. **Add exactly one lint gate and one type gate (lightweight)**
   - Keep it minimal: Ruff + one type checker.
   - Start with non-blocking CI warnings if needed, then tighten.

3. **Refactor only at natural seams (no rewrite)**
   - In `context_gen.py`, extract template loading + cache I/O into separate module.
   - In `store.py`, extract read-only query helpers into a small query service.
   - Do this incrementally only when touching related code.

4. **Enable optional coverage reporting for CI visibility**
   - Ensure dev extras include `pytest-cov` in execution environment.
   - Keep threshold policy conservative at first.

## Final verdict

Project quality is **good and production-promising for a small/medium codebase**: strong test base, clean conceptual architecture, and defensive runtime behavior.  
Main gaps are **DX + maintainability guardrails**, not fundamental design flaws. Addressing the 4 items above should materially improve quality without overengineering.
