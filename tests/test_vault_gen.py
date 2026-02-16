"""Tests for Obsidian vault generation."""
import pytest
from agent_recall.store import MemoryStore
from agent_recall.vault_gen import (
    generate_person, generate_client, generate_vault, trigger_vault_regen,
    _safe_filename,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


# --- Person generation ---

def test_generate_person_basic(store, tmp_path):
    eid = store.resolve_entity("Alice Smith", "person")
    store.set_slot(eid, "role", "Engineer")
    store.set_slot(eid, "email", "alice@example.com")
    path = generate_person(store, eid, tmp_path)
    assert path.exists()
    content = path.read_text()
    assert "# Alice Smith" in content
    assert "tags: [contact]" in content
    assert "**Role:** Engineer" in content
    assert "**Email:** alice@example.com" in content


def test_generate_person_with_clients(store, tmp_path):
    eid = store.resolve_entity("Bob Jones", "person")
    store.set_slot(eid, "role", "Manager")
    store.set_slot(eid, "clients", "acme, beta")
    path = generate_person(store, eid, tmp_path)
    content = path.read_text()
    assert "clients: [acme, beta]" in content


def test_generate_person_observations(store, tmp_path):
    eid = store.resolve_entity("Carol", "person")
    store.set_slot(eid, "role", "Designer")
    store.add_observation(eid, "Great at UX")
    store.add_observation(eid, "Prefers dark mode")
    path = generate_person(store, eid, tmp_path)
    content = path.read_text()
    assert "## Notes" in content
    assert "- Great at UX" in content
    assert "- Prefers dark mode" in content


def test_generate_person_relations(store, tmp_path):
    alice_id = store.resolve_entity("Alice", "person")
    store.set_slot(alice_id, "role", "Engineer")
    acme_id = store.resolve_entity("Acme", "client")
    store.add_relation(alice_id, acme_id, "contact_for")
    path = generate_person(store, alice_id, tmp_path)
    content = path.read_text()
    assert "## Relations" in content
    assert "[[Acme]]" in content


def test_generate_person_output_dir(store, tmp_path):
    """Person file goes into people/ subdir."""
    eid = store.resolve_entity("Test Person", "person")
    store.set_slot(eid, "role", "Tester")
    path = generate_person(store, eid, tmp_path)
    assert path.parent.name == "people"


# --- Client generation ---

def test_generate_client_basic(store, tmp_path):
    cid = store.resolve_entity("Acme Corp", "client")
    store.set_slot(cid, "status", "active")
    store.set_slot(cid, "website", "https://acme.example.com")
    path = generate_client(store, cid, tmp_path)
    assert path.exists()
    content = path.read_text()
    assert "# Acme Corp" in content
    assert "tags: [client]" in content
    assert "status: active" in content
    assert "**Website:** https://acme.example.com" in content


def test_generate_client_contacts(store, tmp_path):
    cid = store.resolve_entity("Acme", "client")
    store.set_slot(cid, "status", "active")
    alice_id = store.resolve_entity("Alice", "person")
    store.set_slot(alice_id, "role", "CTO")
    store.add_relation(alice_id, cid, "contact_for")
    path = generate_client(store, cid, tmp_path)
    content = path.read_text()
    assert "## Contacts" in content
    assert "[[Alice]]" in content


def test_generate_client_logs(store, tmp_path):
    cid = store.resolve_entity("Acme", "client")
    store.set_slot(cid, "status", "active")
    store.add_log(cid, "Project started")
    store.add_log(cid, "First milestone reached")
    path = generate_client(store, cid, tmp_path)
    content = path.read_text()
    assert "## Log" in content
    assert "Project started" in content


def test_generate_client_output_dir(store, tmp_path):
    """Client file goes into clients/ subdir."""
    cid = store.resolve_entity("TestCo", "client")
    store.set_slot(cid, "status", "active")
    path = generate_client(store, cid, tmp_path)
    assert path.parent.name == "clients"


# --- Full vault generation ---

def test_generate_vault_full(store, tmp_path):
    # Create people
    for name in ("Alice", "Bob"):
        eid = store.resolve_entity(name, "person")
        store.set_slot(eid, "role", "Engineer")
    # Create client
    cid = store.resolve_entity("Acme", "client")
    store.set_slot(cid, "status", "active")
    # Create decision document
    store.save_document("adr-001", "decision", "# ADR 001\nUse SQLite")

    stats = generate_vault(store, tmp_path)
    assert stats["people"] == 2
    assert stats["clients"] == 1
    assert stats["decisions"] == 1
    assert (tmp_path / "people" / "Alice.md").exists()
    assert (tmp_path / "people" / "Bob.md").exists()
    assert (tmp_path / "clients" / "Acme.md").exists()
    assert (tmp_path / "decisions" / "adr-001.md").exists()


# --- Rate-limited regen ---

def test_trigger_vault_regen_no_vault_dir(store):
    """Returns False when no vault_dir."""
    assert trigger_vault_regen(store=store, vault_dir=None) is False


def test_trigger_vault_regen_works(store, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "Engineer")

    result = trigger_vault_regen(
        store=store, vault_dir=vault_dir, auto_commit=False,
        rate_file=tmp_path / "rate", force=True
    )
    assert result is True
    assert (vault_dir / "people" / "Alice.md").exists()


def test_trigger_vault_regen_rate_limited(store, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    rate_file = tmp_path / "rate"

    # First call succeeds
    result1 = trigger_vault_regen(
        store=store, vault_dir=vault_dir, auto_commit=False,
        rate_file=rate_file, rate_seconds=300
    )
    assert result1 is True

    # Second call rate-limited
    result2 = trigger_vault_regen(
        store=store, vault_dir=vault_dir, auto_commit=False,
        rate_file=rate_file, rate_seconds=300
    )
    assert result2 is False


def test_trigger_vault_regen_force_bypasses_rate(store, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    rate_file = tmp_path / "rate"

    trigger_vault_regen(
        store=store, vault_dir=vault_dir, auto_commit=False,
        rate_file=rate_file
    )
    # Force bypasses rate limit
    result = trigger_vault_regen(
        store=store, vault_dir=vault_dir, auto_commit=False,
        rate_file=rate_file, force=True
    )
    assert result is True


# --- Filename sanitization ---

def test_safe_filename_normal():
    assert _safe_filename("Alice Smith") == "Alice Smith"


def test_safe_filename_path_traversal():
    assert "/" not in _safe_filename("../../../etc/passwd")
    assert ".." not in _safe_filename("../../../etc/passwd")


def test_safe_filename_slash():
    assert "/" not in _safe_filename("Alice/Bob")


def test_safe_filename_empty():
    assert _safe_filename("") == "unnamed"


def test_safe_filename_dot_prefix():
    assert not _safe_filename(".hidden").startswith(".")


def test_generate_person_path_traversal(store, tmp_path):
    """Entity with path traversal in name doesn't escape vault dir."""
    eid = store.resolve_entity("../../etc/malicious", "person")
    store.set_slot(eid, "role", "Attacker")
    path = generate_person(store, eid, tmp_path)
    # File must stay within people/ dir
    assert path.parent == tmp_path / "people"
    assert ".." not in path.name
