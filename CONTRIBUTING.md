# Contributing to agent-recall

## Quick start

```bash
git clone https://github.com/mnardit/agent-recall.git
cd agent-recall
pip install -e ".[dev]"
pytest
```

## What we're looking for

- Bug fixes (always welcome)
- New prompt templates for AI briefings
- Documentation improvements
- Integration examples (other MCP clients, other LLMs)
- CLI improvements

## Development

- Python 3.10+
- All tests must pass: `pytest`
- Type hints on public API
- Docstrings for public functions

## Running tests

```bash
# All tests
pytest

# Without editable install
PYTHONPATH=. pytest
```

## Not accepting (for now)

- Breaking API changes
- Cloud/hosted features
- Dependencies on specific LLM providers
