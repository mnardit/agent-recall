"""Tests for the Click-based CLI."""
import json
import pytest
from click.testing import CliRunner

from agent_memory.cli import main
from agent_memory.store import MemoryStore


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path):
    """Create a DB and return its path."""
    path = tmp_path / "test.db"
    store = MemoryStore(path)
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "Engineer")
    store.set_slot(eid, "email", "alice@example.com")
    store.add_log(eid, "Started work")
    store.add_observation(eid, "Python expert")
    cid = store.resolve_entity("Acme", "client")
    store.set_slot(cid, "status", "active")
    store.close()
    return str(path)


def test_init(runner, tmp_path):
    path = str(tmp_path / "new.db")
    result = runner.invoke(main, ["--db", path, "init"])
    assert result.exit_code == 0
    assert "initialized" in result.output.lower()


def test_set_and_get(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "set",
                                  "Bob", "person", "role", "Manager"])
    assert result.exit_code == 0
    assert "Bob.role = Manager" in result.output

    result = runner.invoke(main, ["--db", db_path, "get", "Bob", "role"])
    assert result.exit_code == 0
    assert "Manager" in result.output


def test_get_nonexistent(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "get", "Ghost", "role"])
    assert result.exit_code != 0


def test_entity_show(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "entity", "Alice"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "role: Engineer" in result.output


def test_entity_json(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "entity", "Alice", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["name"] == "Alice"
    assert data["slots"]["role"] == "Engineer"


def test_entity_not_found(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "entity", "Ghost"])
    assert result.exit_code != 0


def test_list_all(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "list"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "Acme" in result.output


def test_list_by_type(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "list", "--type", "person"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "Acme" not in result.output


def test_list_json(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 2


def test_search(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "search", "Alice"])
    assert result.exit_code == 0
    assert "Alice" in result.output


def test_history(runner, db_path):
    # Set twice to create history
    runner.invoke(main, ["--db", db_path, "set",
                         "Alice", "person", "role", "Senior Engineer"])
    result = runner.invoke(main, ["--db", db_path, "history", "Alice", "role"])
    assert result.exit_code == 0
    assert "Engineer" in result.output


def test_log_and_logs(runner, db_path):
    runner.invoke(main, ["--db", db_path, "log", "Alice", "Finished task"])
    result = runner.invoke(main, ["--db", db_path, "logs", "Alice"])
    assert result.exit_code == 0
    assert "Finished task" in result.output


def test_logs_json(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "logs", "Alice", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 1


def test_status(runner, db_path):
    result = runner.invoke(main, ["--db", db_path, "status"])
    assert result.exit_code == 0
    assert "Entities:" in result.output
    assert "person:" in result.output
